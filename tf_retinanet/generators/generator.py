import numpy as np
import random
import warnings

import tensorflow as tf

from ..utils.anchors import (
	anchor_targets_bbox,
	anchors_for_shape,
	parse_anchor_parameters,
	guess_shapes
)

from ..utils.image import (
	TransformParameters,
	adjust_transform_for_image,
	apply_transform,
	preprocess_image,
	resize_image,
)
from ..utils.transform import transform_aabb


class Generator(tf.keras.utils.Sequence):
	""" Abstract generator class.
	"""

	def __init__(
		self,
		transform_generator = None,
		batch_size=1,
		group_method='ratio',  # one of 'none', 'random', 'ratio'
		shuffle_groups=True,
		image_min_side=800,
		image_max_side=1333,
		transform_parameters=None,
		compute_anchor_targets=anchor_targets_bbox,
		compute_shapes=guess_shapes,
		preprocess_image=preprocess_image,
		anchors_config=None
	):
		""" Initialize Generator object.

		Args
			transform_generator    : A generator used to randomly transform images and annotations.
			batch_size             : The size of the batches to generate.
			group_method           : Determines how images are grouped together (defaults to 'ratio', one of ('none', 'random', 'ratio')).
			shuffle_groups         : If True, shuffles the groups each epoch.
			image_min_side         : After resizing the minimum side of an image is equal to image_min_side.
			image_max_side         : If after resizing the maximum side is larger than image_max_side, scales down further so that the max side is equal to image_max_side.
			transform_parameters   : The transform parameters used for data augmentation.
			compute_anchor_targets : Function handler for computing the targets of anchors for an image and its annotations.
			compute_shapes         : Function handler for computing the shapes of the pyramid for a given input.
			preprocess_image       : Function handler for preprocessing an image (scaling / normalizing) for passing through a network.
			anchors_config         : Configuration for anchors.
		"""
		self.transform_generator    = transform_generator
		self.batch_size             = int(batch_size)
		self.group_method           = group_method
		self.shuffle_groups         = shuffle_groups
		self.image_min_side         = image_min_side
		self.image_max_side         = image_max_side
		self.transform_parameters   = transform_parameters or TransformParameters()
		self.compute_anchor_targets = compute_anchor_targets
		self.compute_shapes         = compute_shapes
		self.preprocess_image       = preprocess_image
		self.anchor_params = None

		if anchors_config:
			self.anchor_params = parse_anchor_parameters(anchors_config)

		# Define groups.
		self.group_images()

		# Shuffle when initializing.
		if self.shuffle_groups:
			self.on_epoch_end()

	def __from_config__(self, config, preprocess_image):
		""" Initialize Generator object from a configuration.

		Args
			config           : Configuration for the generator.
			preprocess_image : Function handler for preprocessing an image (scaling / normalizing) for passing through a network.
		"""
		Generator.__init__(
			self,
			transform_generator    = config['transform_generator'],
			batch_size             = config['batch_size'],
			group_method           = config['group_method'],
			shuffle_groups         = config['shuffle_groups'],
			image_min_side         = config['image_min_side'],
			transform_parameters   = config['transform_parameters'],
			compute_anchor_targets = config['compute_anchor_targets'],
			compute_shapes         = config['compute_shapes'],
			anchors_config         = config['anchors'],
			preprocess_image       = preprocess_image
		)

	def on_epoch_end(self):
		if self.shuffle_groups:
			random.shuffle(self.groups)

	def size(self):
		""" Size of the dataset.
		"""
		raise NotImplementedError('size method not implemented')

	def num_classes(self):
		""" Number of classes in the dataset.
		"""
		raise NotImplementedError('num_classes method not implemented')

	def has_label(self, label):
		""" Returns True if label is a known label.
		"""
		raise NotImplementedError('has_label method not implemented')

	def has_name(self, name):
		""" Returns True if name is a known class.
		"""
		raise NotImplementedError('has_name method not implemented')

	def name_to_label(self, name):
		""" Map name to label.
		"""
		raise NotImplementedError('name_to_label method not implemented')

	def label_to_name(self, label):
		""" Map label to name.
		"""
		raise NotImplementedError('label_to_name method not implemented')

	def image_aspect_ratio(self, image_index):
		""" Compute the aspect ratio for an image with image_index.
		"""
		raise NotImplementedError('image_aspect_ratio method not implemented')

	def load_image(self, image_index):
		""" Load an image at the image_index.
		"""
		raise NotImplementedError('load_image method not implemented')

	def load_annotations(self, image_index):
		""" Load annotations for an image_index.
		"""
		raise NotImplementedError('load_annotations method not implemented')

	def load_annotations_group(self, group):
		""" Load annotations for all images in group.
		"""
		annotations_group = [self.load_annotations(image_index) for image_index in group]
		for annotations in annotations_group:
			assert(isinstance(annotations, dict)), '\'load_annotations\' should return a list of dictionaries, received: {}'.format(type(annotations))
			assert('labels' in annotations), '\'load_annotations\' should return a list of dictionaries that contain \'labels\' and \'bboxes\'.'
			assert('bboxes' in annotations), '\'load_annotations\' should return a list of dictionaries that contain \'labels\' and \'bboxes\'.'

		return annotations_group

	def filter_annotations(self, image_group, annotations_group, group):
		""" Filter annotations by removing those that are outside of the image bounds or whose width/height < 0.
		"""
		# Test all annotations.
		for index, (image, annotations) in enumerate(zip(image_group, annotations_group)):
			# Test x2 < x1 | y2 < y1 | x1 < 0 | y1 < 0 | x2 <= 0 | y2 <= 0 | x2 >= image.shape[1] | y2 >= image.shape[0].
			invalid_indices = np.where(
				(annotations['bboxes'][:, 2] <= annotations['bboxes'][:, 0]) |
				(annotations['bboxes'][:, 3] <= annotations['bboxes'][:, 1]) |
				(annotations['bboxes'][:, 0] < 0) |
				(annotations['bboxes'][:, 1] < 0) |
				(annotations['bboxes'][:, 2] > image.shape[1]) |
				(annotations['bboxes'][:, 3] > image.shape[0])
			)[0]

			# Delete invalid indices.
			if len(invalid_indices):
				warnings.warn('Image with id {} (shape {}) contains the following invalid boxes: {}.'.format(
					group[index],
					image.shape,
					annotations['bboxes'][invalid_indices, :]
				))
				for k in annotations_group[index].keys():
					annotations_group[index][k] = np.delete(annotations[k], invalid_indices, axis=0)

		return image_group, annotations_group

	def load_image_group(self, group):
		""" Load images for all images in a group.
		"""
		return [self.load_image(image_index) for image_index in group]

	def random_transform_group_entry(self, image, annotations, transform=None):
		""" Randomly transforms image and annotation.
		"""
		# Randomly transform both image and annotations.
		if transform is not None or self.transform_generator:
			if transform is None:
				transform = adjust_transform_for_image(next(self.transform_generator), image, self.transform_parameters.relative_translation)

			# Apply transformation to image.
			image = apply_transform(transform, image, self.transform_parameters)

			# Transform the bounding boxes in the annotations.
			annotations['bboxes'] = annotations['bboxes'].copy()
			for index in range(annotations['bboxes'].shape[0]):
				annotations['bboxes'][index, :] = transform_aabb(transform, annotations['bboxes'][index, :])

		return image, annotations

	def random_transform_group(self, image_group, annotations_group):
		""" Randomly transforms each image and its annotations.
		"""

		assert(len(image_group) == len(annotations_group))

		for index in range(len(image_group)):
			# Transform a single group entry.
			image_group[index], annotations_group[index] = self.random_transform_group_entry(image_group[index], annotations_group[index])

		return image_group, annotations_group

	def resize_image(self, image):
		""" Resize an image using image_min_side and image_max_side.
		"""
		return resize_image(image, min_side=self.image_min_side, max_side=self.image_max_side)

	def preprocess_group_entry(self, image, annotations):
		""" Preprocess image and its annotations.
		"""
		# Preprocess the image.
		image = self.preprocess_image(image)

		# Resize image.
		image, image_scale = self.resize_image(image)

		# Apply resizing to annotations too.
		annotations['bboxes'] *= image_scale

		# Convert to the wanted keras floatx.
		image = tf.keras.backend.cast_to_floatx(image)

		return image, annotations

	def preprocess_group(self, image_group, annotations_group):
		""" Preprocess each image and its annotations in its group.
		"""
		assert(len(image_group) == len(annotations_group))

		for index in range(len(image_group)):
			# Preprocess a single group entry.
			image_group[index], annotations_group[index] = self.preprocess_group_entry(image_group[index], annotations_group[index])

		return image_group, annotations_group

	def group_images(self):
		""" Order the images according to self.order and makes groups of self.batch_size.
		"""
		# Determine the order of the images.
		order = list(range(self.size()))
		if self.group_method == 'random':
			random.shuffle(order)
		elif self.group_method == 'ratio':
			order.sort(key=lambda x: self.image_aspect_ratio(x))

		# Divide into groups, one group = one batch.
		self.groups = [[order[x % len(order)] for x in range(i, i + self.batch_size)] for i in range(0, len(order), self.batch_size)]

	def compute_inputs(self, image_group):
		""" Compute inputs for the network using an image_group.
		"""
		# Get the max image shape.
		max_shape = tuple(max(image.shape[x] for image in image_group) for x in range(3))

		# Construct an image batch object.
		image_batch = np.zeros((self.batch_size,) + max_shape, dtype=tf.keras.backend.floatx())

		# Copy all images to the upper left part of the image batch object.
		for image_index, image in enumerate(image_group):
			image_batch[image_index, :image.shape[0], :image.shape[1], :image.shape[2]] = image

		if tf.keras.backend.image_data_format() == 'channels_first':
			image_batch = image_batch.transpose((0, 3, 1, 2))

		return image_batch

	def generate_anchors(self, image_shape):
		return anchors_for_shape(image_shape, anchor_params=self.anchor_params, shapes_callback=self.compute_shapes)

	def compute_targets(self, image_group, annotations_group):
		""" Compute target outputs for the network using images and their annotations.
		"""
		# Get the max image shape.
		max_shape = tuple(max(image.shape[x] for image in image_group) for x in range(3))
		anchors   = self.generate_anchors(max_shape)

		batches = self.compute_anchor_targets(
			anchors,
			image_group,
			annotations_group,
			self.num_classes()
		)

		return list(batches)

	def compute_input_output(self, group):
		""" Compute inputs and target outputs for the network.
		"""
		# Load images and annotations.
		image_group       = self.load_image_group(group)
		annotations_group = self.load_annotations_group(group)

		# Check validity of annotations.
		image_group, annotations_group = self.filter_annotations(image_group, annotations_group, group)

		# Randomly transform data.
		image_group, annotations_group = self.random_transform_group(image_group, annotations_group)

		# Perform preprocessing steps.
		image_group, annotations_group = self.preprocess_group(image_group, annotations_group)

		# Compute network inputs.
		inputs = self.compute_inputs(image_group)

		# Compute network targets.
		targets = self.compute_targets(image_group, annotations_group)

		return inputs, targets

	def __len__(self):
		"""
		Number of batches for generator.
		"""

		return len(self.groups)

	def __getitem__(self, index):
		"""
		Keras sequence method for generating batches.
		"""
		group = self.groups[index]
		inputs, targets = self.compute_input_output(group)

		return inputs, targets