from torch._C import dtype
import time
from collections import deque
import cv2
cv2.setNumThreads(0)

import os

os.environ["OMP_NUM_THREADS"] = "1"
import numpy as np
import torch
import torch.nn as nn
from torch.nn import functional as F

import gym
import logging
from arguments import get_args
from env import gen_vec_envs
from utils.storage import GlobalRolloutStorage, FIFOMemory
from utils.optimization import get_optimizer
from model import RL_Policy, Local_IL_Policy, Neural_SLAM_Module

import algo

import sys
import matplotlib

if sys.platform == 'darwin':
    matplotlib.use("tkagg")
import matplotlib.pyplot as plt

# plt.ion()
# fig, ax = plt.subplots(1,4, figsize=(10, 2.5), facecolor="whitesmoke")


def get_local_map_boundaries(agent_loc, local_sizes, full_sizes, global_downscaling):
    loc_r, loc_c = agent_loc
    local_w, local_h = local_sizes
    full_w, full_h = full_sizes

    if global_downscaling > 1:
        gx1, gy1 = loc_r - local_w // 2, loc_c - local_h // 2
        gx2, gy2 = gx1 + local_w, gy1 + local_h
        if gx1 < 0:
            gx1, gx2 = 0, local_w
        if gx2 > full_w:
            gx1, gx2 = full_w - local_w, full_w

        if gy1 < 0:
            gy1, gy2 = 0, local_h
        if gy2 > full_h:
            gy1, gy2 = full_h - local_h, full_h
    else:
        gx1, gx2, gy1, gy2 = 0, full_w, 0, full_h

    return [gx1, gx2, gy1, gy2]

def calc_rewards(current_explored_area, previously_explored_area):
    """
    current_explored_area: tensor of size[num_agents, <dimensions of explored area>]
    previously_explored_area: tensor of size[<dimensions of explored area>]
    """
    new_explored_area = torch.zeros_like(current_explored_area)
    for a_ix, a in enumerate(current_explored_area):
        # a is the area explored by one of the agents that
        z = ((torch.sum(current_explored_area, dim = 0) - a) > 0.5).float()
        new_explored_area[a_ix] = ((a - z - previously_explored_area.float()) > 0).float()*10 + a*0.01
    rewards = new_explored_area.sum(dim=tuple(range(1,new_explored_area.dim()))) * 0.0005 # actually 0.0005 but I modified it
    return rewards

def viz(full_map, ep_no, t, res_dir):
    for i in range (0, full_map.shape[0]):
        ag_no = str(i+1)
        directory = '{}/exp_map/{}/{}/'.format(res_dir, ep_no, ag_no)
        if not os.path.exists(directory):
            os.makedirs(directory)

    for i in range (0, full_map.shape[0]):
        ag_no = str(i+1)
        filename = '{}/exp_map/{}/{}/Explored_map-{}.jpg'.format(res_dir, ep_no, ag_no, t)
        viz_map = full_map[i,1].cpu().numpy()
        viz_map = np.stack([viz_map,]*3)
        viz_map = np.transpose(viz_map, (1,2,0))*255
        viz_map = viz_map.astype(np.uint8)
        cv2.imwrite(filename, viz_map)


def main():
    args = get_args()
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    if args.cuda:
        torch.cuda.manual_seed(args.seed)
    # Setup Logging
    log_dir = "{}/models/{}/".format(args.dump_location, args.exp_name)
    dump_dir = "{}/dump/{}/".format(args.dump_location, args.exp_name)

    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    if not os.path.exists("{}/images/".format(dump_dir)):
        os.makedirs("{}/images/".format(dump_dir))

    logging.basicConfig(
        filename=log_dir + 'train.log',
        level=logging.ERROR)
    print("Dumping at {}".format(log_dir))
    print(args)
    logging.info(args)

    # Logging and loss variables
    num_scenes = args.num_processes    ##########????????????
    num_agents = args.num_processes
    num_episodes = int(args.num_episodes)
    device = args.device = torch.device("cuda:0" if args.cuda else "cpu")
    policy_loss = 0

    best_cost = 100000
    costs = deque(maxlen=1000)
    exp_costs = deque(maxlen=1000)
    pose_costs = deque(maxlen=1000)

    g_masks = torch.ones((num_scenes), dtype=torch.float32, device=device)
    l_masks = torch.zeros((num_scenes), dtype=torch.float32, device=device)

    best_local_loss = np.inf
    best_g_reward = -np.inf

    if args.eval:                            ###################????????????
        traj_lengths = args.max_episode_length // args.num_local_steps
        explored_area_log = np.zeros((num_scenes, num_episodes, traj_lengths))
        cum_explored_area_log = np.zeros((num_episodes, traj_lengths))
        explored_ratio_log = np.zeros((num_scenes, num_episodes, traj_lengths))
        cum_explored_ratio_log = np.zeros((num_episodes, traj_lengths))

    g_episode_rewards = deque(maxlen=1000)

    l_action_losses = deque(maxlen=1000)

    g_value_losses = deque(maxlen=1000)
    g_action_losses = deque(maxlen=1000)
    g_dist_entropies = deque(maxlen=1000)

    per_step_g_rewards = deque(maxlen=1000)

    g_process_rewards = np.zeros((num_scenes))

    # Starting environments
    # torch.set_num_threads(1)
    # torch.multiprocessing.set_start_method('spawn')
    # Initialize map variables
    ### Full map consists of 4 channels containing the following:
    ### 1. Obstacle Mapl
    ### 2. Exploread Area
    ### 3. Current Agent Location
    ### 4. Past Agent Location
    ### 5. All Agents Locations

    torch.set_grad_enabled(False)

    # Calculating full and local map sizes
    map_size = args.map_size_cm // args.map_resolution
    full_w, full_h = map_size, map_size
    local_w, local_h = int(full_w / args.global_downscaling), \
                    int(full_h / args.global_downscaling)

    # Initializing full and local map
    full_map = torch.zeros((num_scenes, 5, full_w, full_h), dtype=torch.float32, device=device)  ##############
    local_map = torch.zeros((num_scenes, 4, local_w, local_h), dtype=torch.float32, device=device)    #############

    # Initial full and local pose
    full_pose = torch.zeros((num_scenes, 3), dtype=torch.float32, device=device)  ###############
    local_pose = torch.zeros((num_scenes, 3), dtype=torch.float32, device=device)
    poses = torch.zeros((num_scenes, 3), dtype=torch.float32, device=device)

    # Origin of local map
    origins = np.zeros((num_scenes, 3))   ########?????????

    # Local Map Boundaries
    lmb = np.zeros((num_scenes, 4)).astype(int)     ##########??????????

    ### Planner pose inputs has 7 dimensions
    ### 1-3 store continuous global agent location
    ### 4-7 store local map boundaries
    planner_pose_inputs = np.zeros((num_scenes, 7))   ##########

    # Global policy observation space
    g_observation_space = gym.spaces.Box(0, 1,
                                        (9,
                                        local_w,
                                        local_h), dtype='uint8')

    # Global policy action space
    g_action_space = gym.spaces.Box(low=0.0, high=1.0,
                                    shape=(2,), dtype=np.float32)

    # Local policy observation space
    l_observation_space = gym.spaces.Box(0, 255,
                                        (3,
                                        args.frame_width,
                                        args.frame_width), dtype='uint8')

    # Local and Global policy recurrent layer sizes
    l_hidden_size = args.local_hidden_size
    g_hidden_size = args.global_hidden_size

    # slam
    nslam_module = Neural_SLAM_Module(args).to(device)
    slam_optimizer = get_optimizer(nslam_module.parameters(),
                                args.slam_optimizer)

    # Global policy                 ##########????????
    g_policy = RL_Policy(g_observation_space.shape, g_action_space,
                        base_kwargs={'recurrent': args.use_recurrent_global,
                                    'hidden_size': g_hidden_size,
                                    'downscaling': args.global_downscaling
                                    }).to(device)
    g_agent = algo.PPO(g_policy, args.clip_param, args.ppo_epoch,
                    args.num_mini_batch, args.value_loss_coef,
                    args.entropy_coef, lr=args.global_lr, eps=args.eps,
                    max_grad_norm=args.max_grad_norm)

    # Storage
    g_rollouts = GlobalRolloutStorage(args.num_global_steps,
                                    num_scenes, g_observation_space.shape,
                                    g_action_space, g_policy.rec_state_size,
                                    1, device)

    slam_memory = FIFOMemory(args.slam_memory_size)

    # Loading model   ###########
    if args.load_slam != "0":
        print("Loading slam {}".format(args.load_slam))
        state_dict = torch.load(args.load_slam,
                                map_location=lambda storage, loc: storage)
        nslam_module.load_state_dict(state_dict)

    if not args.train_slam:
        nslam_module.eval()

    if args.load_global != "0":
        print("Loading global {}".format(args.load_global))
        state_dict = torch.load(args.load_global,
                                map_location=lambda storage, loc: storage)
        g_policy.load_state_dict(state_dict)

    if not args.train_global:
        g_policy.eval()

    def init_map_and_pose():
        full_map.fill_(0.)
        full_pose.fill_(0.)
        full_pose[:, :2] = args.map_size_cm / 100.0 / 2.0  ###################

        locs = full_pose.cpu().numpy()
        planner_pose_inputs[:, :3] = locs  ##############
          ####################
        for e in range(num_scenes):
            r, c = locs[e, 1], locs[e, 0]
            loc_r, loc_c = [int(r * 100.0 / args.map_resolution),
                            int(c * 100.0 / args.map_resolution)]

            full_map[e, 2:, loc_r - 1:loc_r + 2, loc_c - 1:loc_c + 2] = 1.0

            lmb[e] = get_local_map_boundaries((loc_r, loc_c),
                                            (local_w, local_h),
                                            (full_w, full_h),
                                            args.global_downscaling)

            planner_pose_inputs[e, 3:] = lmb[e]
            origins[e] = [lmb[e][2] * args.map_resolution / 100.0,
                        lmb[e][0] * args.map_resolution / 100.0, 0.]

        full_map[:,0] = torch.max(full_map[:,0], dim = 0).values
        full_map[:,1] = torch.max(full_map[:,1], dim = 0).values
        full_map[:,4] = torch.max(full_map[:,2], dim = 0).values
        for e in range(num_scenes):
            local_map[e] = full_map[e, :4, lmb[e, 0]:lmb[e, 1], lmb[e, 2]:lmb[e, 3]]
            local_pose[e] = full_pose[e] - \
                            torch.from_numpy(origins[e]).float().to(device)

    for envs in gen_vec_envs(args):
        obs, infos = envs.reset() #########??????????
        previously_explored_area = torch.zeros(infos[0]['explorable_map'].shape, dtype=torch.int32 ,device=device)  ##########
        obs = obs.to(device)   ###########

        # Local policy
        l_policy = Local_IL_Policy(l_observation_space.shape, envs.action_space.n,
                                recurrent=args.use_recurrent_local,
                                hidden_size=l_hidden_size,
                                deterministic=args.use_deterministic_local).to(device)
        local_optimizer = get_optimizer(l_policy.parameters(),
                                        args.local_optimizer)

        if args.load_local != "0":
            print("Loading local {}".format(args.load_local))
            state_dict = torch.load(args.load_local,
                                    map_location=lambda storage, loc: storage)
            l_policy.load_state_dict(state_dict)

        if not args.train_local:
            l_policy.eval()

        with torch.autograd.set_detect_anomaly(False):
            for ep_num in range(num_episodes) :
                obs, infos = envs.reset() #########??????????
                previously_explored_area = torch.zeros(infos[0]['explorable_map'].shape, dtype=torch.int32 ,device=device)  ##########
                obs = obs.to(device)      
                init_map_and_pose()

                # Predict map from frame 1:
                # poses = torch.tensor(
                #     [info['sensor_pose'] for info in infos], 
                #     dtype=torch.float32, device=device)

                for env_idx in range(len(infos)):     ##############????????????????
                    poses[env_idx] = torch.tensor(infos[env_idx]['sensor_pose'], device=device)

                _, _, local_map[:, 0, :, :], local_map[:, 1, :, :], _, local_pose = \
                    nslam_module(obs, obs, poses, local_map[:, 0, :, :],
                                local_map[:, 1, :, :], local_pose)

                # Compute Global policy input
                locs = local_pose.cpu().numpy()
                global_input = torch.zeros(num_scenes, 9, local_w, local_h)   ##############
                global_orientation = torch.zeros(num_scenes, 1).long()

                for e in range(num_scenes):
                    r, c = locs[e, 1], locs[e, 0]
                    loc_r, loc_c = [int(r * 100.0 / args.map_resolution),
                                    int(c * 100.0 / args.map_resolution)]

                    local_map[e, 2:, loc_r - 1:loc_r + 2, loc_c - 1:loc_c + 2] = 1.
                    global_orientation[e] = int((locs[e, 2] + 180.0) / 5.)

                global_input[:, 0:4, :, :] = local_map.detach()
                global_input[:, 4:, :, :] = nn.MaxPool2d(args.global_downscaling)(full_map)

                g_rollouts.obs[0].copy_(global_input)
                g_rollouts.extras[0].copy_(global_orientation)

                # Run Global Policy (global_goals = Long-Term Goal)
                g_value, g_action, g_action_log_prob, g_rec_states = \
                    g_policy.act(
                        g_rollouts.obs[0],
                        g_rollouts.rec_states[0],
                        g_rollouts.masks[0],
                        extras=g_rollouts.extras[0],
                        deterministic=False
                    )

                cpu_actions = nn.Sigmoid()(g_action).cpu().numpy()
                global_goals = [[int(action[0] * local_w), int(action[1] * local_h)]
                                for action in cpu_actions]

                # Compute planner inputs
                planner_inputs = [{} for e in range(num_scenes)]
                for e, p_input in enumerate(planner_inputs):
                    p_input['goal'] = global_goals[e]
                    p_input['map_pred'] = global_input[e, 0, :, :].detach().cpu().numpy()
                    p_input['exp_pred'] = global_input[e, 1, :, :].detach().cpu().numpy()
                    p_input['pose_pred'] = planner_pose_inputs[e]

                # Output stores local goals as well as the the ground-truth action
                output = envs.get_short_term_goal(planner_inputs).long().to(device)

                last_obs = obs.detach()
                local_rec_states = torch.zeros((num_scenes, l_hidden_size), device=device)
                start = time.time()

                total_num_steps = -1
                g_reward = 0

                torch.set_grad_enabled(False)

                
                for step in range(args.max_episode_length):
                    total_num_steps += 1

                    g_step = (step // args.num_local_steps) % args.num_global_steps
                    eval_g_step = step // args.num_local_steps + 1
                    l_step = step % args.num_local_steps
                    # print("l_step = {}, total_num_steps = {}, g_step = {}, eval_g_step = {}".format(l_step, total_num_steps, g_step, eval_g_step))

                    # ------------------------------------------------------------------
                    # Local Policy
                    del last_obs
                    last_obs = obs.detach()
                    local_masks = l_masks
                    local_goals = output[:, :-1]

                    if args.train_local:
                        torch.set_grad_enabled(True)

                    action, action_prob, local_rec_states = l_policy(
                        obs,
                        local_rec_states,
                        local_masks,
                        extras=local_goals,
                    )

                    if args.train_local:
                        action_target = output[:, -1]
                        policy_loss += nn.CrossEntropyLoss()(action_prob, action_target)
                        torch.set_grad_enabled(False)
                    l_action = action.cpu()
                    # ------------------------------------------------------------------

                    # ------------------------------------------------------------------
                    # Env step
                    obs, rew, done, infos = envs.step(l_action)
                    #viz(full_map, ep_num, step, args.dump_location )

                    l_masks = torch.tensor([0 if x else 1 for x in done], 
                                            dtype=torch.float32, device=device)
                    g_masks *= l_masks
                    # ------------------------------------------------------------------

                    # ------------------------------------------------------------------
                    # Reinitialize variables when episode ends
                    if step == args.max_episode_length - 1:  # Last episode step
                        init_map_and_pose()
                        del last_obs
                        last_obs = obs.detach()
                    # ------------------------------------------------------------------

                    # ------------------------------------------------------------------
                    # Neural SLAM Module
                    if args.train_slam:
                        # Add frames to memory
                        for env_idx in range(num_scenes):
                            env_obs = obs[env_idx]
                            env_poses = torch.tensor(infos[env_idx]['sensor_pose'], dtype=torch.float32)
                            env_gt_fp_projs = torch.tensor(infos[env_idx]['fp_proj'], dtype=torch.float32).unsqueeze(0)
                            env_gt_fp_explored = torch.tensor(infos[env_idx]['fp_explored'], dtype=torch.float32).unsqueeze(0)
                            env_gt_pose_err = torch.tensor(infos[env_idx]['pose_err'], dtype=torch.float32)
                            slam_memory.push(
                                (last_obs[env_idx], env_obs, env_poses),
                                (env_gt_fp_projs, env_gt_fp_explored, env_gt_pose_err))
                    
                    obs = obs.to(device)
                    last_obs = last_obs.to(device)
                    # poses = torch.tensor(
                    #     [info['sensor_pose'] for info in infos], 
                    #     dtype=torch.float32, device=device)

                    for env_idx in range(len(infos)):
                        poses[env_idx] = torch.tensor(infos[env_idx]['sensor_pose'], device=device)

                    _, _, local_map[:, 0, :, :], local_map[:, 1, :, :], _, local_pose = \
                        nslam_module(last_obs, obs, poses, local_map[:, 0, :, :],
                                    local_map[:, 1, :, :], local_pose, build_maps=True)

                    locs = local_pose.cpu().numpy()
                    planner_pose_inputs[:, :3] = locs + origins
                    local_map[:, 2, :, :].fill_(0.)  # Resetting current location channel
                    for e in range(num_scenes):
                        r, c = locs[e, 1], locs[e, 0]
                        loc_r, loc_c = [int(r * 100.0 / args.map_resolution),
                                        int(c * 100.0 / args.map_resolution)]

                        local_map[e, 2:, loc_r - 2:loc_r + 3, loc_c - 2:loc_c + 3] = 1.
                    # ------------------------------------------------------------------

                    # ------------------------------------------------------------------
                    # Global Policy
                    if l_step == args.num_local_steps - 1:
                        # print("Updating global policy")
                        # For every global step, update the full and local maps
                        full_pose_cpu = full_pose.cpu()
                        local_pose_cpu = local_pose.cpu()
                        for e in range(num_scenes):
                            full_map[e, :4, lmb[e, 0]:lmb[e, 1], lmb[e, 2]:lmb[e, 3]] = \
                                local_map[e]
                            full_pose_cpu[e] = local_pose_cpu[e] +  \
                                torch.from_numpy(origins[e]).float()

                            locs = full_pose_cpu[e]
                            r, c = locs[1], locs[0]
                            loc_r, loc_c = [int(r * 100.0 / args.map_resolution),
                                            int(c * 100.0 / args.map_resolution)]

                            lmb[e] = get_local_map_boundaries((loc_r, loc_c),
                                                            (local_w, local_h),
                                                            (full_w, full_h),
                                                            args.global_downscaling)

                            planner_pose_inputs[e, 3:] = lmb[e]
                            origins[e] = [lmb[e][2] * args.map_resolution / 100.0,
                                        lmb[e][0] * args.map_resolution / 100.0, 0.]

                        ############################# update
                        full_map[:,0] = torch.max(full_map[:,0], dim = 0).values
                        full_map[:,1] = torch.max(full_map[:,1], dim = 0).values
                        full_map[:,4] = torch.max(full_map[:,2], dim = 0).values

                        for e in range(num_scenes):
                            local_map[e] = full_map[e, :4,
                                        lmb[e, 0]:lmb[e, 1], lmb[e, 2]:lmb[e, 3]]
                            local_pose_cpu[e] = full_pose_cpu[e] -  \
                                torch.from_numpy(origins[e]).float()

                        full_pose = full_pose_cpu.to(device)
                        local_pose = local_pose_cpu.to(device)

                        locs = local_pose_cpu.numpy()
                        for e in range(num_scenes):
                            global_orientation[e] = int((locs[e, 2] + 180.0) / 5.)
                        global_input[:, 0:4, :, :] = local_map
                        global_input[:, 4:, :, :] = \
                            nn.MaxPool2d(args.global_downscaling)(full_map)

                        if False:
                            for i in range(4):
                                ax[i].clear()
                                ax[i].set_yticks([])
                                ax[i].set_xticks([])
                                ax[i].set_yticklabels([])
                                ax[i].set_xticklabels([])
                                ax[i].imshow(global_input.cpu().numpy()[0, 4 + i])
                            plt.gcf().canvas.flush_events()
                            # plt.pause(0.1)
                            fig.canvas.start_event_loop(0.001)
                            plt.gcf().canvas.flush_events()

                        # Get exploration reward and metrics
                        
                        g_reward = torch.tensor(
                            [info['exp_reward'] for info in infos],
                            dtype=torch.float32,device=device)
                        
                        explored_area = torch.tensor(
                            [info['explored_map'] for info in infos],
                            dtype=torch.float32,device=device)
                        explorable_area = torch.tensor(infos[0]['explorable_map'], dtype=torch.float32,device=device)
                        test_rewards = calc_rewards(explored_area * explorable_area, previously_explored_area=previously_explored_area)
                        g_reward = test_rewards  ###############
                        print("Calculating reward = ", g_reward)
                        previously_explored_area = torch.max(explored_area, dim = 0).values

                        if args.eval:
                            g_reward = g_reward*50.0 # Convert reward to area in m2

                        g_process_rewards += g_reward.cpu().numpy()
                        g_total_rewards = g_process_rewards * \
                                        (1 - g_masks.cpu().numpy())
                        g_process_rewards *= g_masks.cpu().numpy()
                        per_step_g_rewards.append(np.mean(g_reward.cpu().numpy()))

                        if np.sum(g_total_rewards) != 0:
                            for tr in g_total_rewards:
                                g_episode_rewards.append(tr) if tr != 0 else None

                        if args.eval:
                            exp_ratio = torch.from_numpy(np.asarray(
                                [infos[env_idx]['exp_ratio'] for env_idx
                                in range(num_scenes)])
                            ).float()

                            for e in range(num_scenes):
                                explored_area_log[e, ep_num, eval_g_step - 1] = \
                                    explored_area_log[e, ep_num, eval_g_step - 2] + \
                                    g_reward[e].cpu().numpy()
                                
                                explored_ratio_log[e, ep_num, eval_g_step - 1] = \
                                    explored_ratio_log[e, ep_num, eval_g_step - 2] + \
                                    exp_ratio[e].cpu().numpy()

                            cum_explored_area_log[ep_num, eval_g_step - 1] = \
                                torch.sum(previously_explored_area*explorable_area).cpu().numpy()
                            
                            cum_explored_ratio_log[ep_num, eval_g_step - 1] = \
                                (torch.sum(previously_explored_area*explorable_area) / torch.sum(explorable_area)).cpu().numpy()

                        # Add samples to global policy storage
                        g_rollouts.insert(
                            global_input, g_rec_states,
                            g_action, g_action_log_prob, g_value,
                            g_reward, g_masks, global_orientation
                        )

                        # Sample long-term goal from global policy
                        g_value, g_action, g_action_log_prob, g_rec_states = \
                            g_policy.act(
                                g_rollouts.obs[g_step + 1],
                                g_rollouts.rec_states[g_step + 1],
                                g_rollouts.masks[g_step + 1],
                                extras=g_rollouts.extras[g_step + 1],
                                deterministic=False
                            )
                        cpu_actions = nn.Sigmoid()(g_action).cpu().numpy()
                        global_goals = [[int(action[0] * local_w),
                                        int(action[1] * local_h)]
                                        for action in cpu_actions]

                        g_reward = 0
                        g_masks = torch.ones((num_scenes), dtype=torch.float32, device=device)
                    # ------------------------------------------------------------------

                    # ------------------------------------------------------------------
                    # Get short term goal
                    planner_inputs = [{} for e in range(num_scenes)]
                    for e, p_input in enumerate(planner_inputs):
                        p_input['map_pred'] = local_map[e, 0, :, :].cpu().numpy()
                        p_input['exp_pred'] = local_map[e, 1, :, :].cpu().numpy()
                        p_input['pose_pred'] = planner_pose_inputs[e]
                        p_input['goal'] = global_goals[e]

                    output = envs.get_short_term_goal(planner_inputs).long().to(device)
                    # ------------------------------------------------------------------

                    ### TRAINING
                    torch.set_grad_enabled(True)
                    # ------------------------------------------------------------------
                    # Train Neural SLAM Module
                    if args.train_slam and len(slam_memory) > args.slam_batch_size:
                        # gen_batch = slam_memory.sample_loader(batch_size=args.slam_batch_size, num_samples=args.slam_iterations, 
                        #                                             num_processes=1, device=device)
                        # for sample in gen_batch:
                        for i in range(args.slam_iterations):
                            sample = slam_memory.sample(batch_size=args.slam_batch_size)
                            #print(total_num_steps)
                            
                            inputs, outputs = sample
                            b_obs_last, b_obs, b_poses = inputs
                            gt_fp_projs, gt_fp_explored, gt_pose_err = outputs
                            b_obs_last, b_obs, b_poses = [x.to(device) for x in [ b_obs_last, b_obs, b_poses]]
                            gt_fp_projs, gt_fp_explored, gt_pose_err = [x.to(device) for x in [gt_fp_projs, gt_fp_explored, gt_pose_err]]

                            b_proj_pred, b_fp_exp_pred, _, _, b_pose_err_pred, _ = \
                                nslam_module(b_obs_last, b_obs, b_poses,
                                            None, None, None,
                                            build_maps=False)
                            loss = 0
                            if args.proj_loss_coeff > 0:
                                proj_loss = F.binary_cross_entropy(b_proj_pred,
                                                                gt_fp_projs)
                                costs.append(proj_loss.item())
                                loss += args.proj_loss_coeff * proj_loss

                            if args.exp_loss_coeff > 0:
                                exp_loss = F.binary_cross_entropy(b_fp_exp_pred,
                                                                gt_fp_explored)
                                exp_costs.append(exp_loss.item())
                                loss += args.exp_loss_coeff * exp_loss

                            if args.pose_loss_coeff > 0:
                                pose_loss = torch.nn.MSELoss()(b_pose_err_pred,
                                                            gt_pose_err)
                                pose_costs.append(args.pose_loss_coeff *
                                                pose_loss.item())
                                loss += args.pose_loss_coeff * pose_loss

                            if args.train_slam:
                                slam_optimizer.zero_grad()
                                loss.backward()
                                slam_optimizer.step()

                            del b_obs_last, b_obs, b_poses
                            del gt_fp_projs, gt_fp_explored, gt_pose_err
                            del b_proj_pred, b_fp_exp_pred, b_pose_err_pred

                    # ------------------------------------------------------------------

                    # ------------------------------------------------------------------
                    # Train Local Policy
                    if (l_step + 1) % args.local_policy_update_freq == 0 \
                            and args.train_local:
                        local_optimizer.zero_grad()
                        policy_loss.backward()
                        local_optimizer.step()
                        l_action_losses.append(policy_loss.item())
                        policy_loss = 0
                        local_rec_states = local_rec_states.detach_()
                    # ------------------------------------------------------------------

                    # ------------------------------------------------------------------
                    # Train Global Policy
                    if g_step % args.num_global_steps == args.num_global_steps - 1 \
                            and l_step == args.num_local_steps - 1:
                        if args.train_global:
                            g_next_value = g_policy.get_value(
                                g_rollouts.obs[-1],
                                g_rollouts.rec_states[-1],
                                g_rollouts.masks[-1],
                                extras=g_rollouts.extras[-1]
                            ).detach()

                            g_rollouts.compute_returns(g_next_value, args.use_gae,
                                                    args.gamma, args.tau)
                            g_value_loss, g_action_loss, g_dist_entropy = \
                                g_agent.update(g_rollouts)
                            g_value_losses.append(g_value_loss)
                            g_action_losses.append(g_action_loss)
                            g_dist_entropies.append(g_dist_entropy)
                        g_rollouts.after_update()
                    # ------------------------------------------------------------------

                    # Finish Training
                    torch.set_grad_enabled(False)
                    # ------------------------------------------------------------------

                    # ------------------------------------------------------------------
                    # Logging
                    if total_num_steps % args.log_interval == 0:
                        end = time.time()
                        time_elapsed = time.gmtime(end - start)
                        log = " ".join([
                            "Time: {0:0=2d}d".format(time_elapsed.tm_mday - 1),
                            "{},".format(time.strftime("%Hh %Mm %Ss", time_elapsed)),
                            "num timesteps {},".format(total_num_steps *
                                                    num_scenes),
                            "FPS {},".format(int(total_num_steps * num_scenes \
                                                / (end - start)))
                        ])

                        log += "\n\tRewards:"

                        if len(g_episode_rewards) > 0:
                            log += " ".join([
                                " Global step mean/med rew:",
                                "{:.4f}/{:.4f},".format(
                                    np.mean(per_step_g_rewards),
                                    np.median(per_step_g_rewards)),
                                " Global eps mean/med/min/max eps rew:",
                                "{:.3f}/{:.3f}/{:.3f}/{:.3f},".format(
                                    np.mean(g_episode_rewards),
                                    np.median(g_episode_rewards),
                                    np.min(g_episode_rewards),
                                    np.max(g_episode_rewards))
                            ])

                        log += "\n\tLosses:"

                        if args.train_local and len(l_action_losses) > 0:
                            log += " ".join([
                                " Local Loss:",
                                "{:.3f},".format(
                                    np.mean(l_action_losses))
                            ])

                        if args.train_global and len(g_value_losses) > 0:
                            log += " ".join([
                                " Global Loss value/action/dist:",
                                "{:.3f}/{:.3f}/{:.3f},".format(
                                    np.mean(g_value_losses),
                                    np.mean(g_action_losses),
                                    np.mean(g_dist_entropies))
                            ])

                        if args.train_slam and len(costs) > 0:
                            log += " ".join([
                                " SLAM Loss proj/exp/pose:"
                                "{:.4f}/{:.4f}/{:.4f}".format(
                                    np.mean(costs),
                                    np.mean(exp_costs),
                                    np.mean(pose_costs))
                            ])

                        print(log)
                        logging.info(log)
                    # ------------------------------------------------------------------

                    # ------------------------------------------------------------------
                    # Save best models
                    if (total_num_steps * num_scenes) % args.save_interval < \
                            num_scenes:

                        # Save Neural SLAM Model
                        # print("trying to save")
                        if len(costs) >= 1000 and np.mean(costs) < best_cost \
                                and not args.eval:
                            print("model_best.slam: current weight is the best weight.")
                            best_cost = np.mean(costs)
                            torch.save(nslam_module.state_dict(),
                                    os.path.join(log_dir, "model_best.slam"))
                        else:
                            # print("model_best.slam: current weight is not the best weight.")
                            pass

                        # Save Local Policy Model
                        if len(l_action_losses) >= 100 and \
                                (np.mean(l_action_losses) <= best_local_loss) \
                                and not args.eval:
                            torch.save(l_policy.state_dict(),
                                    os.path.join(log_dir, "model_best.local"))

                            best_local_loss = np.mean(l_action_losses)

                        # Save Global Policy Model
                        if len(g_episode_rewards) >= 100 and \
                                (np.mean(g_episode_rewards) >= best_g_reward) \
                                and not args.eval:
                            print("model_best.multi_global: current weight is the best weight.")
                            torch.save(g_policy.state_dict(),
                                    os.path.join(log_dir, "model_best.multi_global"))
                            best_g_reward = np.mean(g_episode_rewards)
                        else:
                            # print("model_best.multi_global: current weight is not the best weight.")
                            pass

                    # Save periodic models
                    if (total_num_steps * num_scenes) % args.save_periodic < \
                            num_scenes:
                        step = total_num_steps * num_scenes
                        if args.train_slam:
                            torch.save(nslam_module.state_dict(),
                                    os.path.join(dump_dir,
                                                    "periodic_{}.slam".format(step)))
                        if args.train_local:
                            torch.save(l_policy.state_dict(),
                                    os.path.join(dump_dir,
                                                    "periodic_{}.local".format(step)))
                        if args.train_global:
                            torch.save(g_policy.state_dict(),
                                    os.path.join(dump_dir,
                                                    "periodic_{}.multi_global".format(step)))
                    # ------------------------------------------------------------------

                # Print and save model performance numbers during evaluation
                if args.eval:
                    logfile = open("{}/explored_area.txt".format(dump_dir), "w+")
                    for e in range(num_scenes):
                        for i in range(explored_area_log[e].shape[0]):
                            logfile.write(str(explored_area_log[e, i]) + "\n")
                            logfile.flush()

                    for e in range(cum_explored_ratio_log.shape[0]):
                        logfile.write(str(cum_explored_area_log[e]) + "\n")
                        logfile.flush()

                    logfile.close()

                    logfile = open("{}/explored_ratio.txt".format(dump_dir), "w+")
                    for e in range(num_scenes):
                        for i in range(explored_ratio_log[e].shape[0]):
                            logfile.write(str(explored_ratio_log[e, i]) + "\n")
                            logfile.flush()
                    
                    for e in range(cum_explored_ratio_log.shape[0]):
                        logfile.write(str(cum_explored_ratio_log[e]) + "\n")
                        logfile.flush()

                    logfile.close()

                    log = "Final Exp Area: \n"
                    for i in range(explored_area_log.shape[2]):
                        log += "{:.5f}, ".format(
                            np.mean(explored_area_log[:, :, i]))

                    log += "\nFinal Exp Ratio: \n"
                    for i in range(explored_ratio_log.shape[2]):
                        log += "{:.5f}, ".format(
                            np.mean(explored_ratio_log[:, :, i]))

                    log = "Final Cumulative Exp Area: \n"
                    for e in range(cum_explored_area_log.shape[1]):
                        log += "{:.5f}, ".format(
                            np.mean(cum_explored_area_log[:, i]))

                    log += "\nFinal Cumulative Exp Ratio: \n"
                    for i in range(cum_explored_ratio_log.shape[1]):
                        log += "{:.5f}, ".format(
                            np.mean(cum_explored_ratio_log[:, i]))



                    print(log)
                    logging.info(log)



if __name__ == "__main__":
    main()
