# -*- coding: utf-8 -*-
# Author: Runsheng Xu <rxx3386@ucla.edu>, Yifan Lu
# Modified: sizhewei @ 2022/11/05
# License: TDG-Attribution-NonCommercial-NoDistrib

"""
Dataset class for intermediate fusion with past k frames
"""
from collections import OrderedDict
import os
import numpy as np
import torch
import math
import copy

import opencood.data_utils.post_processor as post_processor
import opencood.utils.pcd_utils as pcd_utils
from opencood.utils.keypoint_utils import bev_sample, get_keypoints
from opencood.data_utils.datasets import basedataset
from opencood.data_utils.pre_processor import build_preprocessor
from opencood.hypes_yaml.yaml_utils import load_yaml
from opencood.utils.pcd_utils import \
	mask_points_by_range, mask_ego_points, shuffle_points, \
	downsample_lidar_minimum
from opencood.data_utils.augmentor.data_augmentor import DataAugmentor
from opencood.utils.transformation_utils import tfm_to_pose, x1_to_x2, x_to_world
from opencood.utils.pose_utils import add_noise_data_dict, remove_z_axis
from opencood.utils.common_utils import read_json
from opencood.utils import box_utils
# from opencood.models.sub_modules.box_align_v2 import box_alignment_relative_sample_np


class IntermediateFusionDatasetMultisweep(basedataset.BaseDataset):
	"""
	This class is for intermediate fusion where each vehicle transmit the
	deep features to ego.
	"""
	def __init__(self, params, visualize, train=True):
		self.params = params
		self.visualize = visualize
		self.train = train

		self.pre_processor = None
		self.post_processor = None
		self.data_augmentor = DataAugmentor(params['data_augment'],
											train)

		if 'num_sweep_frames' in params:    # number of frames we use in LSTM
			self.k = params['num_sweep_frames']
		else:
			self.k = 0

		if 'time_delay' in params:          # number of time delay
			self.tau = params['time_delay'] 
		else:
			self.tau = 0

		assert 'proj_first' in params['fusion']['args']
		if params['fusion']['args']['proj_first']:
			self.proj_first = True
		else:
			self.proj_first = False

		if self.train:
			root_dir = params['root_dir']
		else:
			root_dir = params['validate_dir']
		
		print("Dataset dir:", root_dir)

		if 'train_params' not in params or\
				'max_cav' not in params['train_params']:
			self.max_cav = 5
		else:
			self.max_cav = params['train_params']['max_cav']

		# first load all paths of different scenarios
		scenario_folders = sorted([os.path.join(root_dir, x)
								   for x in os.listdir(root_dir) if
								   os.path.isdir(os.path.join(root_dir, x))])
		scenario_folders_name = sorted([x
								   for x in os.listdir(root_dir) if
								   os.path.isdir(os.path.join(root_dir, x))])
		'''
		scenario_database Structure: 
		{
			scenario_id : {
				cav_1 : {
					'ego' : true / false , 
					timestamp1 : {
						yaml: path,
						lidar: path, 
						cameras: list of path
					},
					...
				},
				...
			}
		}
		'''
		self.scenario_database = OrderedDict()
		self.len_record = []

		# loop over all scenarios
		for (i, scenario_folder) in enumerate(scenario_folders):
			self.scenario_database.update({i: OrderedDict()})

			# at least 1 cav should show up
			cav_list = sorted([x 
							   for x in os.listdir(scenario_folder) if 
							   os.path.isdir(os.path.join(scenario_folder, x))])
			assert len(cav_list) > 0

			# loop over all CAV data
			for (j, cav_id) in enumerate(cav_list):
				if j > self.max_cav - 1:
					print('too many cavs')
					break
				self.scenario_database[i][cav_id] = OrderedDict()

				# save all yaml files to the dictionary
				cav_path = os.path.join(scenario_folder, cav_id)

				# use the frame number as key, the full path as the values
				yaml_files = \
					sorted([os.path.join(cav_path, x)
							for x in os.listdir(cav_path) if
							x.endswith('.yaml')])
				timestamps = self.extract_timestamps(yaml_files)	

				# Assume all cavs will have the same timestamps length. Thus
				# we only need to calculate for the first vehicle in the
				# scene.
				if j == 0:  # ego 
					# we regard the agent with the minimum id as the ego
					self.scenario_database[i][cav_id]['ego'] = True
					num_ego_timestamps = len(timestamps) - (self.tau + self.k - 1)		# 从第 tau+k 个往后, store 0 时刻的 time stamp
					if not self.len_record:
						self.len_record.append(num_ego_timestamps)
					else:
						prev_last = self.len_record[-1]
						self.len_record.append(prev_last + num_ego_timestamps)

				else:
					self.scenario_database[i][cav_id]['ego'] = False

				for timestamp in timestamps:
					self.scenario_database[i][cav_id][timestamp] = \
						OrderedDict()

					yaml_file = os.path.join(cav_path,
											 timestamp + '.yaml')
					lidar_file = os.path.join(cav_path,
											  timestamp + '.pcd')
					# camera_files = self.load_camera_files(cav_path, timestamp)

					self.scenario_database[i][cav_id][timestamp]['yaml'] = \
						yaml_file
					self.scenario_database[i][cav_id][timestamp]['lidar'] = \
						lidar_file
					# self.scenario_database[i][cav_id][timestamp]['camera0'] = \
						# camera_files
					

		# if project first, cav's lidar will first be projected to
		# the ego's coordinate frame. otherwise, the feature will be
		# projected instead.
		# TODO: check pre_process & post_processor 是否符合新的数据结构 
		self.pre_processor = build_preprocessor(params['preprocess'],
												train)
		self.post_processor = post_processor.build_postprocessor(
			params['postprocess'],
			train)

		print("OPV2V Multi-sweep dataset with \
			non-ego cavs' {} time delay and \
				past {} frames collected initialized! \
					{} samples totally!".format(self.tau, self.k, self.len_record[-1]))

	def retrieve_base_data(self, idx):
		"""
		Given the index, return the corresponding data.

		Parameters
		----------
		idx : int
			Index given by dataloader.

		Returns
		-------
		data : dict
			The dictionary contains loaded yaml params and lidar data for
			each cav.
			Structure: 
			{
				cav_id_1 : {
					'ego' : true,
					curr : {						#      |       | label
						'params': (yaml),
						'lidar_np': (numpy),
						'timestamp': string
					},
					past_k : {		# (k) totally
						[0]:{		(0)				# pose | lidar | 
							'params': (yaml),
							'lidar_np': (numpy),
							'timestamp': string
						},
						[1] : {},	(0-1)			# pose | lidar |
						...,						# pose | lidar |
						[k-1] : {} (0-(k-1))		# pose | lidar |
					}
					
				}, 
				cav_id_2 : {
					'ego': false, 
					curr : 							#      |       | label
							'params': (yaml),
							'lidar_np': (numpy),
							'timestamp': string
					},
					past_k: {		# (k) totally
						[0] : {		(0 - \tau - 1)	# pose | lidar |
							'params': (yaml),
							'lidar_np': (numpy),
							'timestamp': string
						}			
						..., 						# pose | lidar |
						[k-1]:{}	(0 - \tau - k)	# pose | lidar |
					},
				}, 
				...
			}
		"""
		# we loop the accumulated length list to get the scenario index
		scenario_index = 0
		for i, ele in enumerate(self.len_record):
			if idx < ele:
				scenario_index = i
				break
		scenario_database = self.scenario_database[scenario_index]

		data = OrderedDict()
		# load files for all CAVs
		for cav_id, cav_content in scenario_database.items():
			'''
			cav_content 
			{
				'ego' : true / false , 
				timestamp1 : {
					yaml: path,
					lidar: path, 
					cameras: list of path
				},
				...
			},
			'''
			data[cav_id] = OrderedDict()
			data[cav_id]['ego'] = cav_content['ego']
			data[cav_id]['past_k'] = OrderedDict()
			
			# current frame (for lable use)
			data[cav_id]['curr'] = {}
			timestamp_index = idx + i if scenario_index == 0 else \
						idx + i - self.len_record[scenario_index - 1]
			timestamp_index = timestamp_index + self.tau + self.k - 1
			timestamp_key = list(cav_content.items())[timestamp_index][0]
			data[cav_id]['curr']['params'] = \
					load_yaml(cav_content[timestamp_key]['yaml'])
			data[cav_id]['curr']['lidar_np'] = \
					pcd_utils.pcd_to_np(cav_content[timestamp_key]['lidar'])
			data[cav_id]['curr']['timestamp'] = \
					timestamp_key

			# past k frames
			if data[cav_id]['ego']:
				temp = self.tau
			else:
				temp = 0
			for i in range(self.k):
				# check the timestamp index
				data[cav_id]['past_k'][i] = OrderedDict()
				timestamp_index = idx + i if scenario_index == 0 else \
					idx + i - self.len_record[scenario_index - 1]
				timestamp_index = timestamp_index + self.k - 1 - i + temp
				timestamp_key = list(cav_content.items())[timestamp_index][0]
				# load the corresponding data into the dictionary
				data[cav_id]['past_k'][i]['params'] = \
					load_yaml(cav_content[timestamp_key]['yaml'])
				data[cav_id]['past_k'][i]['lidar_np'] = \
					pcd_utils.pcd_to_np(cav_content[timestamp_key]['lidar'])
				data[cav_id]['past_k'][i]['timestamp'] = \
					timestamp_key

		return data


	def __getitem__(self, idx):
		'''
		return: 
		TODO: 注释写一下dict structure
		dict structure:
		{
			'ego_feature'
			'non_ego_feature'
			'labels'
		}
		'''
		# base_data_dict
		base_data_dict = self.retrieve_base_data(idx)
		
		processed_data_dict = OrderedDict()
		processed_data_dict['ego'] = {}

		# first find the ego vehicle's lidar pose
		ego_id = -1
		ego_lidar_pose = []
		for cav_id, cav_content in base_data_dict.items():
			if cav_content['ego']:
				ego_id = cav_id
				ego_lidar_pose = cav_content['params']['lidar_pose']
				# ego_lidar_pose_clean = cav_content['params']['lidar_pose_clean']
				break	
		assert cav_id == list(base_data_dict.keys())[
			0], "The first element in the OrderedDict must be ego"
		assert ego_id != -1
		assert len(ego_lidar_pose) > 0

		too_far = []
		lidar_pose_list = []
		cav_id_list = []

		if self.visualize:
			projected_lidar_stack = []

		# loop over all CAVs to process information
		for cav_id, selected_cav_base in base_data_dict.items():
			# check if the cav is within the communication range with ego
			# for non-ego cav, we use the latest frame's pose
			distance = \
				math.sqrt( \
					(selected_cav_base['past_k'][0]['params']['lidar_pose'][0] - ego_lidar_pose[0]) ** 2 + \
					(selected_cav_base['past_k'][0]['params']['lidar_pose'][1] - ego_lidar_pose[1]) ** 2)

			# if distance is too far, we will just skip this agent
			if distance > self.params['comm_range']:
				too_far.append(cav_id)
				continue

			lidar_pose_list.append(selected_cav_base['params']['lidar_pose']) # 6dof pose
			cav_id_list.append(cav_id)  

		ego_features = []
		processed_features = [] # non-ego
		object_stack = []
		object_id_stack = []
		
		for cav_id in cav_id_list:
			selected_cav_base = base_data_dict[cav_id]
			# TODO: 完成 get_itme_single_car
			selected_cav_processed = self.get_item_single_car(
				selected_cav_base,
				ego_lidar_pose, 
				cav_id == ego_id, 
				idx
			)
			# label
			object_stack.append(selected_cav_processed['object_bbx_center'])
			object_id_stack += selected_cav_processed['object_ids']
			# features
			processed_features.append(selected_cav_processed['processed_features'])

			if self.visualize:
				projected_lidar_stack.append(
					selected_cav_processed['projected_lidar'])
		
			# obj: selected_cav_processed structure:
			# {	'object_bbx_center': object_bbx_center[object_bbx_mask == 1],	# ego only
			# 	'object_ids': object_ids,										# ego only
			# 	'projected_lidar': projected_lidar,
			# 	'processed_features': processed_lidar,
			# 	'transformation_matrix': transformation_matrix } 
			
		########## Added by Yifan Lu 2022.4.5 ################
		# filter those out of communicate range
		# then we can calculate get_pairwise_transformation
		for cav_id in too_far:
			base_data_dict.pop(cav_id)
		
		pairwise_t_matrix = \
			self.get_pairwise_transformation(base_data_dict,
											 self.max_cav) # np.tile(np.eye(4), (max_cav, max_cav, 1, 1)) # (L, L, 4, 4)

		lidar_poses = np.array(lidar_pose_list).reshape(-1, 6)  # [N_cav, 6]
		# lidar_poses_clean = np.array(lidar_pose_clean_list).reshape(-1, 6)  # [N_cav, 6]
		######################################################

		# ############ for disconet ###########
		# if self.kd_flag:
		# 	stack_lidar_np = np.vstack(projected_lidar_clean_list)
		# 	stack_lidar_np = mask_points_by_range(stack_lidar_np,
		# 								self.params['preprocess'][
		# 									'cav_lidar_range'])
		# 	stack_feature_processed = self.pre_processor.preprocess(stack_lidar_np)

		# exclude all repetitive objects    
		unique_indices = \
			[object_id_stack.index(x) for x in set(object_id_stack)]
		object_stack = np.vstack(object_stack)
		object_stack = object_stack[unique_indices]

		# make sure bounding boxes across all frames have the same number
		object_bbx_center = \
			np.zeros((self.params['postprocess']['max_num'], 7))
		mask = np.zeros(self.params['postprocess']['max_num'])
		object_bbx_center[:object_stack.shape[0], :] = object_stack
		mask[:object_stack.shape[0]] = 1

		# merge preprocessed features from different cavs into the same dict
		cav_num = len(processed_features)

		merged_feature_dict = self.merge_features_to_dict(processed_features)

		# generate the anchor boxes
		anchor_box = self.post_processor.generate_anchor_box()

		# generate targets label
		label_dict = \
			self.post_processor.generate_label(
				gt_box_center=object_bbx_center,
				anchors=anchor_box,
				mask=mask)

		processed_data_dict['ego'].update(
			{'object_bbx_center': object_bbx_center,
			 'object_bbx_mask': mask,
			 'object_ids': [object_id_stack[i] for i in unique_indices],
			 'anchor_box': anchor_box,
			 'processed_lidar': merged_feature_dict,
			 'label_dict': label_dict,
			 'cav_num': cav_num,
			 'pairwise_t_matrix': pairwise_t_matrix,
			 'lidar_poses_clean': lidar_poses_clean,
			 'lidar_poses': lidar_poses})

		if self.kd_flag:
			processed_data_dict['ego'].update({'teacher_processed_lidar':
				stack_feature_processed})

		if self.visualize:
			processed_data_dict['ego'].update({'origin_lidar':
				np.vstack(
					projected_lidar_stack)})


		processed_data_dict['ego'].update({'sample_idx': idx,
											'cav_id_list': cav_id_list})

		return processed_data_dict


	def get_item_single_car(self, selected_cav_base, ego_pose, idx):
		"""
		Project the lidar and bbx to ego space first, and then do clipping.

		Parameters
		----------
		selected_cav_base : dict
			The dictionary contains a single CAV's raw information, 
			structure: {
				'ego' : true / false,
				curr : {										#      |       | label
					'params': (yaml),
					'lidar_np': (numpy),
					'timestamp': string
				},
				past_k : {		# (k) totally
					[0]:{		(0) / (0 - \tau - 1)			# pose | lidar | 
						'params': (yaml),
						'lidar_np': (numpy),
						'timestamp': string
					},
					[1] : {},	(0-1) / (0-\tau-(k-1))			# pose | lidar |
					...,										# pose | lidar |
					[k-1] : {} 	(0-(k-1)) / (0-\tau-k)			# pose | lidar |
				}	
			}, 
		ego_pose : list, length 6
			The ego vehicle lidar pose under world coordinate.
		# ego_pose_clean : list, length 6
		# 	only used for gt box generation
		ego_flag : bool
			ego cav: True, non-ego cav: False
		
		idx: int,
			debug use.

		Returns
		-------
		selected_cav_processed : dict
			The dictionary contains the cav's processed information.
		"""
		selected_cav_processed = {}
		
		# past k poses
		past_k_poses = []
		# past k lidars
		past_k_lidars = []
		# past k timestamps
		past_k_timestamps = []
		for i in range(self.k):
			transformation_matrix = \
            	x1_to_x2(selected_cav_base['past_k']['params']['lidar_pose'], ego_pose) # T_ego_cav
			lidar_np = selected_cav_base['lidar_np']
			lidar_np = shuffle_points(lidar_np)
			# remove points that hit itself
			lidar_np = mask_ego_points(lidar_np)
			# project the lidar to ego space
			# x,y,z in ego space
			projected_lidar = \
				box_utils.project_points_by_matrix_torch(lidar_np[:, :3],
															transformation_matrix)
			lidar_np = mask_points_by_range(lidar_np,
													self.params['preprocess'][
														'cav_lidar_range'])
			processed_lidar = self.pre_processor.preprocess(lidar_np)


		# curr label at ego coordinates
		transformation_matrix = \
            x1_to_x2(selected_cav_base['curr']['params']['lidar_pose'], ego_pose) # T_ego_cav
		object_bbx_center, object_bbx_mask, object_ids = \
			self.generate_object_center([selected_cav_base['curr']], ego_pose)  # opencood/data_utils/post_processor/base_postprocessor.py
		
		selected_cav_processed.update(
            {'object_bbx_center': object_bbx_center[object_bbx_mask == 1],
             'object_ids': object_ids,
             'projected_lidar': projected_lidar,
             'processed_features': processed_lidar,
             'transformation_matrix': transformation_matrix})

		return selected_cav_processed

	@staticmethod
	def return_timestamp_key_async(cav_content, timestamp_index):
		"""
		Given the timestamp index, return the correct timestamp key, e.g.
		2 --> '000078'.

		Parameters
		----------
		scenario_database : OrderedDict
			The dictionary contains all contents in the current scenario.

		timestamp_index : int
			The index for timestamp.

		Returns
		-------
		timestamp_key : str
			The timestamp key saved in the cav dictionary.
		"""
		# retrieve the correct index
		timestamp_key = list(cav_content.items())[timestamp_index][0]

		return timestamp_key

	@staticmethod
	def merge_features_to_dict(processed_feature_list):
		"""
		Merge the preprocessed features from different cavs to the same
		dictionary.

		Parameters
		----------
		processed_feature_list : list
			A list of dictionary containing all processed features from
			different cavs.

		Returns
		-------
		merged_feature_dict: dict
			key: feature names, value: list of features.
		"""

		merged_feature_dict = OrderedDict()

		for i in range(len(processed_feature_list)):
			for feature_name, feature in processed_feature_list[i].items():
				if feature_name not in merged_feature_dict:
					merged_feature_dict[feature_name] = []
				if isinstance(feature, list):
					merged_feature_dict[feature_name] += feature
				else:
					merged_feature_dict[feature_name].append(feature) # merged_feature_dict['coords'] = [f1,f2,f3,f4]
		return merged_feature_dict

	def collate_batch_train(self, batch):
		# Intermediate fusion is different the other two
		output_dict = {'ego': {}}

		object_bbx_center = []
		object_bbx_mask = []
		object_ids = []
		processed_lidar_list = []
		# used to record different scenario
		record_len = []
		label_dict_list = []
		lidar_pose_list = []
		lidar_pose_clean_list = []
		
		# pairwise transformation matrix
		pairwise_t_matrix_list = []

		if self.kd_flag:
			teacher_processed_lidar_list = []
		if self.visualize:
			origin_lidar = []

		for i in range(len(batch)):
			ego_dict = batch[i]['ego']
			object_bbx_center.append(ego_dict['object_bbx_center'])
			object_bbx_mask.append(ego_dict['object_bbx_mask'])
			object_ids.append(ego_dict['object_ids'])
			lidar_pose_list.append(ego_dict['lidar_poses']) # ego_dict['lidar_pose'] is np.ndarray [N,6]
			lidar_pose_clean_list.append(ego_dict['lidar_poses_clean'])

			processed_lidar_list.append(ego_dict['processed_lidar']) # different cav_num, ego_dict['processed_lidar'] is list.
			record_len.append(ego_dict['cav_num'])

			label_dict_list.append(ego_dict['label_dict'])
			pairwise_t_matrix_list.append(ego_dict['pairwise_t_matrix'])

			if self.kd_flag:
				teacher_processed_lidar_list.append(ego_dict['teacher_processed_lidar'])

			if self.visualize:
				origin_lidar.append(ego_dict['origin_lidar'])

		# convert to numpy, (B, max_num, 7)
		object_bbx_center = torch.from_numpy(np.array(object_bbx_center))
		object_bbx_mask = torch.from_numpy(np.array(object_bbx_mask))


		# example: {'voxel_features':[np.array([1,2,3]]),
		# np.array([3,5,6]), ...]}
		merged_feature_dict = self.merge_features_to_dict(processed_lidar_list)

		# [sum(record_len), C, H, W]
		processed_lidar_torch_dict = \
			self.pre_processor.collate_batch(merged_feature_dict)
		# [2, 3, 4, ..., M], M <= max_cav
		record_len = torch.from_numpy(np.array(record_len, dtype=int))
		# [[N1, 6], [N2, 6]...] -> [[N1+N2+...], 6]
		lidar_pose = torch.from_numpy(np.concatenate(lidar_pose_list, axis=0))
		lidar_pose_clean = torch.from_numpy(np.concatenate(lidar_pose_clean_list, axis=0))
		label_torch_dict = \
			self.post_processor.collate_batch(label_dict_list)

		# (B, max_cav)
		pairwise_t_matrix = torch.from_numpy(np.array(pairwise_t_matrix_list))

		# add pairwise_t_matrix to label dict
		label_torch_dict['pairwise_t_matrix'] = pairwise_t_matrix
		label_torch_dict['record_len'] = record_len

		# object id is only used during inference, where batch size is 1.
		# so here we only get the first element.
		output_dict['ego'].update({'object_bbx_center': object_bbx_center,
								   'object_bbx_mask': object_bbx_mask,
								   'processed_lidar': processed_lidar_torch_dict,
								   'record_len': record_len,
								   'label_dict': label_torch_dict,
								   'object_ids': object_ids[0],
								   'pairwise_t_matrix': pairwise_t_matrix,
								   'lidar_pose_clean': lidar_pose_clean,
								   'lidar_pose': lidar_pose})


		if self.visualize:
			origin_lidar = \
				np.array(downsample_lidar_minimum(pcd_np_list=origin_lidar))
			origin_lidar = torch.from_numpy(origin_lidar)
			output_dict['ego'].update({'origin_lidar': origin_lidar})
		
		if self.kd_flag:
			teacher_processed_lidar_torch_dict = \
				self.pre_processor.collate_batch(teacher_processed_lidar_list)
			output_dict['ego'].update({'teacher_processed_lidar':teacher_processed_lidar_torch_dict})

		if self.params['preprocess']['core_method'] == 'SpVoxelPreprocessor' and \
			(output_dict['ego']['processed_lidar']['voxel_coords'][:, 0].max().int().item() + 1) != record_len.sum().int().item():
			return None

		return output_dict

	def collate_batch_test(self, batch):
		assert len(batch) <= 1, "Batch size 1 is required during testing!"
		output_dict = self.collate_batch_train(batch)
		if output_dict is None:
			return None

		# check if anchor box in the batch
		if batch[0]['ego']['anchor_box'] is not None:
			output_dict['ego'].update({'anchor_box':
				torch.from_numpy(np.array(
					batch[0]['ego'][
						'anchor_box']))})

		# save the transformation matrix (4, 4) to ego vehicle
		# transformation is only used in post process (no use.)
		# we all predict boxes in ego coord.
		transformation_matrix_torch = \
			torch.from_numpy(np.identity(4)).float()
		transformation_matrix_clean_torch = \
			torch.from_numpy(np.identity(4)).float()

		output_dict['ego'].update({'transformation_matrix':
									   transformation_matrix_torch,
									'transformation_matrix_clean':
									   transformation_matrix_clean_torch,})

		output_dict['ego'].update({
			"sample_idx": batch[0]['ego']['sample_idx'],
			"cav_id_list": batch[0]['ego']['cav_id_list']
		})

		# output_dict['ego'].update({'veh_frame_id': batch[0]['ego']['veh_frame_id']})

		return output_dict

	def post_process(self, data_dict, output_dict):
		"""
		Process the outputs of the model to 2D/3D bounding box.

		Parameters
		----------
		data_dict : dict
			The dictionary containing the origin input data of model.

		output_dict :dict
			The dictionary containing the output of the model.

		Returns
		-------
		pred_box_tensor : torch.Tensor
			The tensor of prediction bounding box after NMS.
		gt_box_tensor : torch.Tensor
			The tensor of gt bounding box.
		"""
		pred_box_tensor, pred_score = \
			self.post_processor.post_process(data_dict, output_dict)
		gt_box_tensor = self.post_processor.generate_gt_bbx(data_dict)

		return pred_box_tensor, pred_score, gt_box_tensor

	def get_pairwise_transformation(self, base_data_dict, max_cav):
		"""
		Get pair-wise transformation matrix accross different agents.

		Parameters
		----------
		base_data_dict : dict
			Key : cav id, item: transformation matrix to ego, lidar points.

		max_cav : int
			The maximum number of cav, default 5

		Return
		------
		pairwise_t_matrix : np.array
			The pairwise transformation matrix across each cav.
			shape: (L, L, 4, 4), L is the max cav number in a scene
			pairwise_t_matrix[i, j] is Tji, i_to_j
		"""
		pairwise_t_matrix = np.tile(np.eye(4), (max_cav, max_cav, 1, 1)) # (L, L, 4, 4)

		if self.proj_first:
			# if lidar projected to ego first, then the pairwise matrix
			# becomes identity
			# no need to warp again in fusion time.

			# pairwise_t_matrix[:, :] = np.identity(4)
			return pairwise_t_matrix
		else:
			t_list = []

			# save all transformation matrix in a list in order first.
			for cav_id, cav_content in base_data_dict.items():
				lidar_pose = cav_content['params']['lidar_pose']
				t_list.append(x_to_world(lidar_pose))  # Twx

			for i in range(len(t_list)):
				for j in range(len(t_list)):
					# identity matrix to self
					if i != j:
						# i->j: TiPi=TjPj, Tj^(-1)TiPi = Pj
						# t_matrix = np.dot(np.linalg.inv(t_list[j]), t_list[i])
						t_matrix = np.linalg.solve(t_list[j], t_list[i])  # Tjw*Twi = Tji
						pairwise_t_matrix[i, j] = t_matrix

		return pairwise_t_matrix
