"""
## Setup
"""

import os
import time

import numpy as np
import sys
import random

start = time.time()

os.environ["KERAS_BACKEND"] = "tensorflow"

np.set_printoptions(threshold=sys.maxsize)

# Make sure we are able to handle large datasets
import resource

low, high = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (high, high))

import math
import matplotlib.pyplot as plt
import tensorflow as tf
import tensorflow_datasets as tfds

import keras
from keras import ops
from keras import layers
from keras import callbacks
from tf_keras import mixed_precision

from sklearn.manifold import TSNE

from utils import *

os.environ['CUDA_VISIBLE_DEVICES'] = '-1'  # run on CPU

os.environ['XLA_FLAGS'] = '--xla_gpu_strict_conv_algorithm_picker=false'

if tf.config.list_physical_devices('GPU'):
    print("Using GPU...")
else:
    print("Using CPU...")

# Mixed precision policy
policy = mixed_precision.Policy('mixed_float16')
mixed_precision.set_global_policy(policy)
print("Using mixed precision...")

"""
## Hyperparameter setup
"""

M = 64
E_thres = 0.15
Kt = 100
batch_size = 100
num_epochs = 30
temperature = 0.1

"""
## Dataset
"""

# Adjust the paths to be relative to the current script location
gdata_path = os.path.join('..', 'data', 'tremor_gdata.pickle')
tremor_gdata = unpickle_data(gdata_path)

# gdataset = form_unlabeled_dataset(tremor_gdata, E_thres, Kt)

with open("unlabeled_data.pickle", 'rb') as f:
    gdataset = pkl.load(f)

print(np.shape(gdataset))

gdataset = gdataset[:2000]

print(np.shape(gdataset))

gdataset = tf.data.Dataset.from_tensor_slices(gdataset)
gdataset = gdataset.shuffle(buffer_size=len(gdataset)).batch(batch_size).prefetch(buffer_size=tf.data.AUTOTUNE)

print(gdataset.element_spec)

"""
## Augmentations
"""


class Augmentation:
    def __init__(self, jitter_factor=0.1, rotation_angle=np.pi/2, block_size_ratio=0.1, crop_ratio=0.5,
                 lambda_amp=0.5,
                 n_perm_seg=4, min_seg_size=125):
        # Set default parameters for each augmentation
        self.jitter_factor = jitter_factor
        self.rotation_angle = rotation_angle
        self.block_size_ratio = block_size_ratio
        self.crop_ratio = crop_ratio
        self.lambda_amp = lambda_amp
        self.n_perm_seg = n_perm_seg
        self.min_seg_size = min_seg_size

    def jitter(self, data):
        """Add random noise to the data."""
        noise = tf.random.normal(shape=tf.shape(data), mean=0.0, stddev=self.jitter_factor)
        return data + noise

    def left_to_right_flipping(self, data):
        """
        Perform left-to-right flipping of 3D accelerometer data.
        The time-series data is reversed along the time axis (axis 1).
        """
        return tf.reverse(data, axis=[1])

    def bidirectional_flipping(self, data):
        """
        Perform bidirectional flipping of 3D accelerometer data.
        The time-series data is mirrored along the channel axis (axis 2).
        """
        return data * -1

    def random_channel_permutation(self, data):
        """
        Randomly permutes the channels (X, Y, Z) in the 3D accelerometer data.
        The time dimension remains unchanged during this augmentation.
        """
        channels = tf.shape(data)[2]

        # Generate a random permutation of channel indices
        permuted_indices = tf.random.shuffle(tf.range(channels))

        # Apply the permutation along the channel dimension
        return tf.gather(data, permuted_indices, axis=2)

    # def rotate_axis(self, data):
    #     theta = tf.random.uniform([], -np.pi / 2, np.pi / 2)
    #     rotation_matrix = tf.convert_to_tensor([
    #         [tf.cos(theta), -tf.sin(theta), 0],
    #         [tf.sin(theta), tf.cos(theta), 0],
    #         [0, 0, 1]
    #     ])
    #     return tf.linalg.matmul(data, rotation_matrix)

    def rotate_axis(self, data):
        # Generate a random rotation matrix for each sample in the batch
        def rotate_single_sample(sample):
            # Generate a random axis for rotation (normalized) per sample
            axis = tf.random.uniform([3], minval=-1.0, maxval=1.0)
            axis = axis / tf.norm(axis)

            # Generate a random rotation angle per sample
            angle = tf.random.uniform([], minval=-self.rotation_angle, maxval=self.rotation_angle)

            # Compute components of the rotation matrix using the axis-angle formula
            cos_angle = tf.cos(angle)
            sin_angle = tf.sin(angle)
            one_minus_cos = 1.0 - cos_angle

            x, y, z = axis[0], axis[1], axis[2]

            # Rotation matrix for an arbitrary axis (Rodrigues' rotation formula)
            rotation_matrix = tf.convert_to_tensor([
                [cos_angle + x * x * one_minus_cos,
                 x * y * one_minus_cos - z * sin_angle,
                 x * z * one_minus_cos + y * sin_angle],

                [y * x * one_minus_cos + z * sin_angle,
                 cos_angle + y * y * one_minus_cos,
                 y * z * one_minus_cos - x * sin_angle],

                [z * x * one_minus_cos - y * sin_angle,
                 z * y * one_minus_cos + x * sin_angle,
                 cos_angle + z * z * one_minus_cos]
            ])

            # Apply the rotation matrix to the sample
            return tf.linalg.matmul(sample, rotation_matrix)

        # Apply the rotate_single_sample function to each sample in the batch using tf.map_fn
        rotated_batch = tf.map_fn(rotate_single_sample, data)

        return rotated_batch

    def blockout(self, data):
        """
        Apply blockout augmentation by randomly setting a block of neighboring elements to zero.
        """
        batch_size, time_steps, channels = tf.shape(data)[0], tf.shape(data)[1], tf.shape(data)[2]
        block_size = tf.cast(self.block_size_ratio * tf.cast(time_steps, tf.float32), tf.int32)

        # Randomly select the starting index for the block for each sample in the batch
        block_start = tf.random.uniform(shape=(batch_size,), minval=0, maxval=time_steps - block_size, dtype=tf.int32)

        time_indices = tf.range(time_steps)

        block_start_expanded = tf.expand_dims(block_start, axis=-1)

        # Create the mask by zeroing out the block
        block_mask = tf.logical_and(time_indices >= block_start_expanded,
                                    time_indices < block_start_expanded + block_size)

        block_mask = tf.cast(tf.expand_dims(block_mask, axis=-1), tf.float32)

        data_masked = data * (1 - block_mask)

        return data_masked

    @tf.function
    def crop_and_resize(self, data):
        """
        Randomly crop a portion of the time-series data and resize it to the original length.
        """
        batch_size, time_steps, channels = tf.shape(data)[0], tf.shape(data)[1], tf.shape(data)[2]

        # Ensure at least half of the original time series is kept
        min_crop_size = tf.cast((1 - self.crop_ratio) * tf.cast(time_steps, tf.float32), tf.int32)
        max_crop_size = time_steps

        # Randomly select the crop size
        crop_size = tf.random.uniform(shape=(), minval=min_crop_size, maxval=max_crop_size, dtype=tf.int32)

        # Randomly select the starting index for the crop for each sample in the batch
        crop_start = tf.random.uniform(shape=(), minval=0, maxval=time_steps - crop_size, dtype=tf.int32)

        # Create the cropped data
        def crop_fn(i):
            return data[i, crop_start:crop_start + crop_size, :]

        cropped_data = tf.map_fn(crop_fn, tf.range(batch_size), fn_output_signature=tf.float32)

        # Resize cropped data to original time_steps using linear interpolation
        cropped_data = tf.expand_dims(cropped_data, axis=2)
        resized_data = tf.image.resize(cropped_data, [time_steps, 1], method="bilinear")
        resized_data = tf.squeeze(resized_data, axis=2)

        return resized_data

    def magnitude_warping(self, data, num_periods=3):
        """
        Apply magnitude warping to the input data by scaling the signal with a sine wave matrix.
        """
        batch_size, time_steps, channels = tf.shape(data)[0], tf.shape(data)[1], tf.shape(data)[2]

        # Create a time array for the sine wave (normalized to 0 to 2π)
        time_steps_range = tf.linspace(0.0, 2.0 * np.pi * num_periods, time_steps)
        time_steps_range = tf.reshape(time_steps_range, (time_steps, 1))

        # Generate random phase shifts for each channel in each batch
        random_phase_shifts = tf.random.uniform(shape=(batch_size, channels), minval=0.0, maxval=2.0 * np.pi)

        def apply_sine_warping(inputs):
            signal, phase_shifts = inputs
            # Create the sine wave for each channel with the corresponding phase shift
            sine_waves = tf.sin(time_steps_range + phase_shifts)
            # Scale the sine wave matrix and add 1
            sine_warping = 1.0 + self.lambda_amp * sine_waves
            # Apply the sine warping to the signal
            return signal * sine_warping

        # Apply the sine warping to each sample in the batch using tf.map_fn
        warped_data = tf.map_fn(apply_sine_warping, (data, random_phase_shifts), fn_output_signature=tf.float32)

        return warped_data

    # @tf.function
    # def time_warping(self, data):
    #     """
    #     Apply time warping to the time-series data.
    #     - Compress some parts of the data by discarding points.
    #     - Stretch some parts using linear interpolation.
    #     """
    #     batch_size, time_steps, channels = tf.shape(data)[0], tf.shape(data)[1], tf.shape(data)[2]
    #
    #     # Generate a random vector of length time_steps with values -1, 0, 1 for each sample in the batch
    #     random_warp = tf.random.uniform(shape=(time_steps,), minval=-1, maxval=2, dtype=tf.int32)
    #     random_warp = tf.tile(tf.expand_dims(random_warp, axis=0), [batch_size, 1])
    #
    #     def warp_single_sample(inputs):
    #         sample_data, warp_vector = inputs
    #         warped_data = []
    #
    #         for i in range(time_steps):
    #             # Compress (discard): if warp_vector[i] == -1, skip the current time step
    #             if warp_vector[i] == -1:
    #                 continue
    #             # Stretch (interpolate): if warp_vector[i] == 1, average the current and the next time step
    #             elif warp_vector[i] == 1:
    #                 if i < time_steps - 1:
    #                     interpolated_value = (sample_data[i, :] + sample_data[i + 1, :]) / 2
    #                     warped_data.append(sample_data[i, :])  # Add the original point
    #                     warped_data.append(interpolated_value)  # Add the interpolated point
    #                 else:
    #                     warped_data.append(sample_data[i, :])  # At the end, no interpolation possible
    #             # No action: if warp_vector[i] == 0, keep the data as-is
    #             else:
    #                 warped_data.append(sample_data[i, :])
    #
    #         # Convert warped data back to tensor
    #         warped_data = tf.stack(warped_data)
    #
    #         # Resize only the time dimension (axis 0) to match the original time_steps using bilinear interpolation
    #         warped_data_resized = tf.image.resize(tf.expand_dims(warped_data, axis=1), [time_steps, 1],
    #                                               method="bilinear")
    #
    #         return tf.squeeze(warped_data_resized, axis=1)
    #
    #     # Apply the time warping to each sample in the batch
    #     warped_batch = tf.map_fn(warp_single_sample, (data, random_warp), fn_output_signature=tf.float32)
    #
    #     return warped_batch

    # @tf.function
    # def time_warping(self, data):
    #     """
    #     Apply time warping to the time-series data.
    #     - Compress some parts of the data by discarding points.
    #     - Stretch some parts using linear interpolation.
    #     """
    #     batch_size, time_steps, channels = tf.shape(data)[0], tf.shape(data)[1], tf.shape(data)[2]
    #
    #     # Generate a random vector of length time_steps with values -1, 0, 1 for each sample in the batch
    #     random_warp = tf.random.uniform(shape=(time_steps,), minval=-1, maxval=2, dtype=tf.int32)
    #     random_warp = tf.tile(tf.expand_dims(random_warp, axis=0), [batch_size, 1])
    #
    #     def warp_single_sample(inputs):
    #         sample_data, warp_vector = inputs
    #
    #         # Initialize a TensorArray to store warped data
    #         warped_data = tf.TensorArray(dtype=tf.float32, size=0, dynamic_size=True)
    #
    #         def loop_body(i, warped_data):
    #             warp_value = warp_vector[i]
    #
    #             # Compress (discard): if warp_value == -1, skip the current time step
    #             def compress_step(warped_data):
    #                 return warped_data
    #
    #             # Stretch (interpolate): if warp_value == 1, average the current and the next time step
    #             def stretch_step(warped_data):
    #                 next_i = tf.minimum(i + 1, time_steps - 1)
    #                 interpolated_value = (sample_data[i, :] + sample_data[next_i, :]) / 2
    #                 warped_data = warped_data.write(warped_data.size(), sample_data[i, :])
    #                 warped_data = warped_data.write(warped_data.size(), interpolated_value)
    #                 return warped_data
    #
    #             # No action: if warp_value == 0, keep the data as-is
    #             def keep_step(warped_data):
    #                 warped_data = warped_data.write(warped_data.size(), sample_data[i, :])
    #                 return warped_data
    #
    #             # Choose action based on the warp_value using tf.cond
    #             warped_data = tf.cond(
    #                 warp_value == -1,
    #                 lambda: compress_step(warped_data),
    #                 lambda: tf.cond(warp_value == 1, lambda: stretch_step(warped_data), lambda: keep_step(warped_data))
    #             )
    #
    #             return i + 1, warped_data
    #
    #         # Use tf.while_loop to iterate over time_steps
    #         i = tf.constant(0)
    #         _, warped_data = tf.while_loop(lambda i, _: i < time_steps, loop_body, [i, warped_data])
    #
    #         # Stack the warped_data TensorArray back into a tensor
    #         warped_data = warped_data.stack()
    #
    #         # Resize only the time dimension (axis 0) to match the original time_steps using bilinear interpolation
    #         warped_data_resized = tf.image.resize(tf.expand_dims(warped_data, axis=1), [time_steps, 1],
    #                                               method="bilinear")
    #
    #         return tf.squeeze(warped_data_resized, axis=1)
    #
    #     # Apply the time warping to each sample in the batch
    #     warped_batch = tf.map_fn(warp_single_sample, (data, random_warp), fn_output_signature=tf.float32)
    #     warped_batch = tf.reshape(warped_batch, [batch_size, time_steps, channels])
    #
    #     return warped_batch

    def time_warping(self, data):
        """
        Apply time warping to the time-series data.
        - Compress some parts of the data by discarding points.
        - Stretch some parts using linear interpolation.
        """
        batch_size, time_steps, channels = tf.shape(data)[0], tf.shape(data)[1], tf.shape(data)[2]

        tf.print("Running on:", data.device)

        # Generate a random vector of length time_steps with values -1, 0, 1 for each sample in the batch
        random_warp = tf.random.uniform(shape=(time_steps,), minval=-1, maxval=2, dtype=tf.int32)

        # Precompute the number of elements that will be kept or interpolated
        num_kept = tf.reduce_sum(tf.cast(random_warp == 0, tf.int32))
        num_stretched = tf.reduce_sum(
            tf.cast(random_warp == 1, tf.int32)) * 2  # Each stretch adds an interpolated point
        final_size = num_kept + num_stretched

        random_warp = tf.tile(tf.expand_dims(random_warp, axis=0), [batch_size, 1])

        def warp_single_sample(inputs):
            sample_data, warp_vector = inputs

            # Initialize a TensorArray with a fixed size
            warped_data = tf.TensorArray(dtype=tf.float32, size=final_size, dynamic_size=False)

            def loop_body(i, j, warped_data):
                warp_value = warp_vector[i]

                # Compress (discard): if warp_value == -1, skip the current time step
                def compress_step(j, warped_data):
                    return j, warped_data

                # Stretch (interpolate): if warp_value == 1, average the current and the next time step
                def stretch_step(j, warped_data):
                    next_i = tf.minimum(i + 1, time_steps - 1)
                    interpolated_value = (sample_data[i, :] + sample_data[next_i, :]) / 2
                    warped_data = warped_data.write(j, sample_data[i, :])  # Add original point
                    warped_data = warped_data.write(j + 1, interpolated_value)  # Add interpolated point
                    return j + 2, warped_data

                # No action: if warp_value == 0, keep the data as-is
                def keep_step(j, warped_data):
                    warped_data = warped_data.write(j, sample_data[i, :])  # Add original point
                    return j + 1, warped_data

                # Choose action based on the warp_value using tf.cond
                j, warped_data = tf.cond(
                    warp_value == -1,
                    lambda: compress_step(j, warped_data),
                    lambda: tf.cond(warp_value == 1, lambda: stretch_step(j, warped_data),
                                    lambda: keep_step(j, warped_data))
                )

                return i + 1, j, warped_data

            # Use tf.while_loop to iterate over time_steps
            i = tf.constant(0)
            j = tf.constant(0)  # Tracks the index for the preallocated TensorArray
            _, _, warped_data = tf.while_loop(lambda i, j, _: i < time_steps, loop_body, [i, j, warped_data])

            # Stack the warped_data TensorArray back into a tensor
            warped_data = warped_data.stack()

            # Resize only the time dimension (axis 0) to match the original time_steps using bilinear interpolation
            warped_data_resized = tf.image.resize(tf.expand_dims(warped_data, axis=1), [time_steps, 1],
                                                  method="bilinear")

            return tf.squeeze(warped_data_resized, axis=1)

        # Apply the time warping to each sample in the batch
        warped_batch = tf.map_fn(warp_single_sample, (data, random_warp), fn_output_signature=tf.float32)
        warped_batch = tf.reshape(warped_batch, [batch_size, time_steps, channels])

        return warped_batch

    def random_smoothing(self, data):
        """
        Apply random smoothing to time-series data using a FIR filter with a random smoothing factor.
        """
        batch_size, time_steps, channels = tf.shape(data)[0], tf.shape(data)[1], tf.shape(data)[2]

        lambda_val = tf.random.uniform(shape=[], minval=0, maxval=1, dtype=tf.float32)

        # FIR filter coefficients based on λ
        filter_coeffs = tf.stack([lambda_val / 2, 1 - lambda_val, lambda_val / 2])
        filter_coeffs = tf.reshape(filter_coeffs, [3, 1, 1])  # Shape: [filter_size, 1, 1]

        data_reshaped = tf.reshape(data, [batch_size * channels, time_steps, 1])

        smoothed_data = tf.nn.conv1d(data_reshaped, filters=filter_coeffs, stride=1, padding='SAME')
        smoothed_data = tf.reshape(smoothed_data, [batch_size, time_steps, channels])

        return smoothed_data

    def permute_segments(self, data):
        """
        Permute segments of the input data along the time axis.
        """
        batch_size, time_steps, channels = tf.shape(data)[0], tf.shape(data)[1], tf.shape(data)[2]

        # Ensure that the time_steps can be divided into n_perm_seg segments
        segment_size = time_steps // self.n_perm_seg

        # Reshape data to have an extra dimension for the segments
        reshaped_data = tf.reshape(data, [batch_size, self.n_perm_seg, segment_size, channels])

        # Generate a random permutation of the segment indices for each sample in the batch
        permuted_indices = tf.map_fn(
            lambda _: tf.random.shuffle(tf.range(self.n_perm_seg)),
            tf.zeros([batch_size], dtype=tf.int32),
            fn_output_signature=tf.int32
        )

        # Gather the segments in the new permuted order for each batch
        permuted_data = tf.map_fn(
            lambda x: tf.gather(x[0], x[1]),
            (reshaped_data, permuted_indices),
            fn_output_signature=tf.float32
        )

        # Reshape back to the original shape (batch_size, time_steps, channels)
        permuted_data = tf.reshape(permuted_data, [batch_size, time_steps, channels])

        return permuted_data

    def get_augmenter(self):
        """Combine several augmentations into a single sequential model."""
        return keras.Sequential(
            [
                # layers.Lambda(self.jitter),
                # layers.Lambda(self.left_to_right_flipping),
                # layers.Lambda(self.bidirectional_flipping),
                # layers.Lambda(self.random_channel_permutation),
                layers.Lambda(self.rotate_axis),
                # layers.Lambda(self.blockout),
                # layers.Lambda(self.crop_and_resize),
                # layers.Lambda(self.magnitude_warping),
                # layers.Lambda(self.time_warping),
                # layers.Lambda(self.random_smoothing),
                # layers.Lambda(self.permute_segments),
            ]
        )


# Visualization function
def visualize_augmentations(gdataset, augmentation, num_windows):
    augmenter = augmentation.get_augmenter()

    gdataset_np = next(iter(gdataset)).numpy()  # Convert from tensor to numpy array

    # Choose 3 random windows from the batch
    random_indices = random.sample(range(len(gdataset_np)), num_windows)
    original_windows = [gdataset_np[i] for i in random_indices]

    # Apply augmentation to the selected windows
    augmented_windows = augmenter(np.array(original_windows))

    # Plotting
    fig, axs = plt.subplots(2, num_windows, figsize=(15, 6))

    for i in range(num_windows):
        # Plot original data
        axs[0, i].plot(original_windows[i][:, 0], label='X', color='r')  # X-axis (red)
        axs[0, i].plot(original_windows[i][:, 1], label='Y', color='g')  # Y-axis (green)
        axs[0, i].plot(original_windows[i][:, 2], label='Z', color='b')  # Z-axis (blue)
        axs[0, i].set_title("Original")
        axs[0, i].legend(loc='upper right')

        # Plot augmented data
        axs[1, i].plot(augmented_windows[i][:, 0], label='X', color='r')  # X-axis (red)
        axs[1, i].plot(augmented_windows[i][:, 1], label='Y', color='g')  # Y-axis (green)
        axs[1, i].plot(augmented_windows[i][:, 2], label='Z', color='b')  # Z-axis (blue)
        axs[1, i].set_title("Augmented")
        axs[1, i].legend(loc='upper right')

    plt.tight_layout()
    plt.show()


augmentation = Augmentation()
# visualize_augmentations(gdataset, augmentation, num_windows=3)

"""
## Encoder architecture
"""


# Define the encoder architecture
def embeddings_function(M):
    return keras.Sequential(
        [
            # Layer 1
            layers.ZeroPadding1D(padding=1),
            layers.Conv1D(filters=32, kernel_size=8, padding='valid'),
            layers.LeakyReLU(negative_slope=0.2),
            layers.MaxPooling1D(pool_size=2),

            # Layer 2
            layers.ZeroPadding1D(padding=1),
            layers.Conv1D(filters=32, kernel_size=8, padding='valid'),
            layers.LeakyReLU(negative_slope=0.2),
            layers.MaxPooling1D(pool_size=2),

            # Layer 3
            layers.ZeroPadding1D(padding=1),
            layers.Conv1D(filters=16, kernel_size=16, padding='valid'),
            layers.LeakyReLU(negative_slope=0.2),
            layers.MaxPooling1D(pool_size=2),

            # Layer 4
            layers.ZeroPadding1D(padding=1),
            layers.Conv1D(filters=16, kernel_size=16, padding='valid'),
            layers.LeakyReLU(negative_slope=0.2),
            layers.MaxPooling1D(pool_size=2),

            # Flatten and Dense layer to get M-dimensional output
            layers.Flatten(),
            layers.Dense(M),
        ],
        name="embeddings_function",
    )


"""
## Self-supervised model for contrastive pretraining
"""


# Define the contrastive model with model-subclassing
class ContrastiveModel(keras.Model):
    def __init__(self):
        super().__init__()

        self.temperature = temperature
        self.contrastive_augmenter = augmentation.get_augmenter()
        self.encoder = embeddings_function(M)

        self.current_index = tf.Variable(0, trainable=False, dtype=tf.int32)
        self.similarity_values = tf.Variable(tf.zeros((600, 2)), trainable=False)

        # Non-linear MLP as projection head
        self.projection_head = keras.Sequential(
            [
                keras.Input(shape=(M,)),
                layers.Dense(64, activation="linear"),
                # layers.BatchNormalization(),
                layers.Dropout(0.2),
                layers.Dense(32, activation="linear"),
                # layers.Dropout(0.2),
                layers.Dense(32),  # Keep this linear layer at the end for better contrastive loss
            ],
            name="projection_head",
        )

        # self.projection_head = keras.Sequential(
        #     [
        #         keras.Input(shape=(M, 1)),  # Assuming M is the feature dimension
        #
        #         # 1D Convolution to capture temporal dependencies
        #         layers.Conv1D(64, kernel_size=3, strides=1, padding="same", activation="linear"),
        #         # layers.BatchNormalization(),
        #         layers.Dropout(0.2),
        #
        #         # Another convolutional layer for deeper feature extraction
        #         layers.Conv1D(32, kernel_size=3, strides=1, padding="same", activation="linear"),
        #         # layers.BatchNormalization(),
        #
        #         # Global average pooling to reduce dimensionality
        #         layers.GlobalAveragePooling1D(),
        #
        #         # Dense layer to output final embeddings
        #         layers.Dense(32, activation="linear"),
        #         layers.Dense(32),  # Keep this linear layer at the end for contrastive loss
        #     ],
        #     name="projection_head",
        # )

        self.encoder.summary()
        self.projection_head.summary()

    def compile(self, contrastive_optimizer, **kwargs):
        super().compile(**kwargs)

        self.contrastive_optimizer = contrastive_optimizer

        self.contrastive_loss_tracker = keras.metrics.Mean(name="c_loss")
        self.contrastive_accuracy = keras.metrics.SparseCategoricalAccuracy(
            name="c_acc"
        )

    @property
    def metrics(self):
        return [
            self.contrastive_loss_tracker,
            self.contrastive_accuracy
        ]

    def contrastive_loss_with_regularization(self, projections_1, projections_2, regularization_weight=0.0):
        # InfoNCE loss (information noise-contrastive estimation)
        # NT-Xent loss (normalized temperature-scaled cross entropy)

        # Cosine similarity: the dot product of the l2-normalized feature vectors
        # projections_1 = ops.normalize(projections_1, axis=1)
        # projections_2 = ops.normalize(projections_2, axis=1)
        projections_1 = tf.nn.l2_normalize(projections_1, axis=1)
        projections_2 = tf.nn.l2_normalize(projections_2, axis=1)
        similarities = (
                ops.matmul(projections_1, ops.transpose(projections_2)) / self.temperature
        )

        # The similarity between the representations of two augmented views of the
        # same image should be higher than their similarity with other views
        batch_size = ops.shape(projections_1)[0]
        contrastive_labels = ops.arange(batch_size)
        self.contrastive_accuracy.update_state(contrastive_labels, similarities)
        self.contrastive_accuracy.update_state(
            contrastive_labels, ops.transpose(similarities)
        )

        # Mask to exclude the diagonal (positive pairs)
        mask = tf.eye(batch_size)
        negative_similarities = tf.where(mask == 0, similarities, 0)

        # Regularization term: penalize negative similarities that are too high
        regularization_term = tf.reduce_sum(
            tf.square(negative_similarities))  # L2 regularization on the negative similarities
        regularization_loss = regularization_weight * regularization_term

        # Calculate positive pair similarities
        loss_1_2 = keras.losses.sparse_categorical_crossentropy(contrastive_labels, similarities, from_logits=True)
        loss_2_1 = keras.losses.sparse_categorical_crossentropy(contrastive_labels, ops.transpose(similarities),
                                                                from_logits=True)

        # Combine the regularization term with the contrastive loss
        contrastive_loss = (loss_1_2 + loss_2_1) / 2
        combined_loss = contrastive_loss + regularization_loss

        return combined_loss

    def contrastive_loss(self, projections_1, projections_2):
        # InfoNCE loss (information noise-contrastive estimation)
        # NT-Xent loss (normalized temperature-scaled cross entropy)

        # Cosine similarity: the dot product of the l2-normalized feature vectors
        projections_1 = ops.normalize(projections_1, axis=1)
        projections_2 = ops.normalize(projections_2, axis=1)
        similarities = (
                ops.matmul(projections_1, ops.transpose(projections_2)) / self.temperature
        )

        # The similarity between the representations of two augmented views of the
        # same image should be higher than their similarity with other views
        batch_size = ops.shape(projections_1)[0]
        contrastive_labels = ops.arange(batch_size)
        self.contrastive_accuracy.update_state(contrastive_labels, similarities)
        self.contrastive_accuracy.update_state(
            contrastive_labels, ops.transpose(similarities)
        )

        # The temperature-scaled similarities are used as logits for cross-entropy
        # a symmetrized version of the loss is used here
        loss_1_2 = keras.losses.sparse_categorical_crossentropy(
            contrastive_labels, similarities, from_logits=True
        )
        loss_2_1 = keras.losses.sparse_categorical_crossentropy(
            contrastive_labels, ops.transpose(similarities), from_logits=True
        )
        return (loss_1_2 + loss_2_1) / 2

    def compute_similarity_metrics(self, projections_1, projections_2):
        # Normalize the projections
        projections_1 = tf.nn.l2_normalize(projections_1, axis=1)
        projections_2 = tf.nn.l2_normalize(projections_2, axis=1)

        # Compute cosine similarities
        similarities = tf.matmul(projections_1, tf.transpose(projections_2))

        # Get dynamic shape of the batch (since similarities.shape[0] might return None)
        batch_size = tf.shape(similarities)[0]

        # Extract positive pair similarities (diagonal)
        positive_similarities = tf.linalg.diag_part(similarities)

        # Extract negative pair similarities (off-diagonal)
        negative_similarities = tf.reshape(similarities, [-1])

        # Create a mask to remove diagonal elements (positive pairs)
        negative_mask = tf.not_equal(
            tf.tile(tf.range(batch_size), [batch_size]),
            tf.repeat(tf.range(batch_size), batch_size)
        )

        # Apply the mask to extract only negative similarities
        negative_similarities = tf.boolean_mask(negative_similarities, negative_mask)

        return tf.reduce_mean(positive_similarities), tf.reduce_mean(negative_similarities)


    def train_step(self, data):
        # Each window is augmented twice, differently
        augmented_data_1 = self.contrastive_augmenter(data, training=True)
        augmented_data_2 = self.contrastive_augmenter(data, training=True)

        with tf.GradientTape() as tape:
            # Pass both augmented versions of the images through the encoder
            features_1 = self.encoder(augmented_data_1, training=True)
            features_2 = self.encoder(augmented_data_2, training=True)

            # The representations are passed through a projection MLP
            projections_1 = self.projection_head(features_1, training=True)
            projections_2 = self.projection_head(features_2, training=True)

            # Compute the contrastive loss
            contrastive_loss = self.contrastive_loss_with_regularization(projections_1, projections_2)

            # SIMILARITY METRICS
            positive_sim, negative_sim = self.compute_similarity_metrics(projections_1, projections_2)
            new_values = tf.stack([positive_sim, negative_sim])
            self.similarity_values[self.current_index].assign(new_values)
            self.current_index.assign_add(1)

        # Compute gradients of the contrastive loss and update the encoder and projection head
        gradients = tape.gradient(
            contrastive_loss,
            self.encoder.trainable_weights + self.projection_head.trainable_weights,
        )
        self.contrastive_optimizer.apply_gradients(
            zip(
                gradients,
                self.encoder.trainable_weights + self.projection_head.trainable_weights,
            )
        )

        # Update the contrastive loss tracker for monitoring
        self.contrastive_loss_tracker.update_state(contrastive_loss)

        return {m.name: m.result() for m in self.metrics}

    # def test_step(self, data):
    #
    #     # Augment the windows twice for contrastive testing
    #     augmented_data_1 = self.contrastive_augmenter(data, training=False)
    #     augmented_data_2 = self.contrastive_augmenter(data, training=False)
    #
    #     # Extract features from both augmented views using the encoder
    #     features_1 = self.encoder(augmented_data_1, training=False)
    #     features_2 = self.encoder(augmented_data_2, training=False)
    #
    #     # Pass the features through the projection head
    #     projections_1 = self.projection_head(features_1, training=False)
    #     projections_2 = self.projection_head(features_2, training=False)
    #
    #     # Calculate contrastive loss (during testing, we don't apply gradients)
    #     contrastive_loss = self.contrastive_loss(projections_1, projections_2)
    #     self.contrastive_loss_tracker.update_state(contrastive_loss)
    #
    #     # Return only contrastive loss tracker for evaluation
    #     return {m.name: m.result() for m in self.metrics}


# Contrastive pretraining
pretraining_model = ContrastiveModel()
pretraining_model.compile(
    contrastive_optimizer=keras.optimizers.Adam(learning_rate=1e-3)
)

early_stopping = callbacks.EarlyStopping(
    monitor="c_loss",
    patience=10,
    mode="min",
    verbose=1,
    start_from_epoch=0,
    restore_best_weights=True
)

pretraining_history = pretraining_model.fit(
    gdataset, epochs=num_epochs, batch_size=batch_size, callbacks=[early_stopping]
)

print(
    "Maximal contrastive accuracy: {:.2f}%".format(
        max(pretraining_history.history["c_acc"]) * 100
    )
)

print(
    "Minimum contrastive loss: {:.2f}".format(
        min(pretraining_history.history["c_loss"])
    )
)

for pos_sim, neg_sim in pretraining_model.similarity_values:
    print("Positive similarity: {:.2f}, Negative similarity: {:.2f}".format(pos_sim, neg_sim))

pretraining_model.get_layer("embeddings_function").save_weights("embeddings.weights.h5")

embeddings = pretraining_model.get_layer("embeddings_function").predict(gdataset)

print("Embeddings shape: {}".format(embeddings.shape))


def visualize_embeddings(embeddings, n_components=2, perplexity=50, learning_rate=1000, n_iter=500):
    """
    Visualize the embeddings using t-SNE.
    """
    # Initialize t-SNE model
    tsne = TSNE(n_components=n_components, perplexity=perplexity, learning_rate=learning_rate, n_iter=n_iter,
                random_state=42)

    # Apply t-SNE to embeddings
    reduced_embeddings = tsne.fit_transform(embeddings)

    # 2D Visualization
    if n_components == 2:
        plt.figure(figsize=(10, 8))
        plt.scatter(reduced_embeddings[:, 0], reduced_embeddings[:, 1])
        plt.title("2D t-SNE Visualization of Embeddings")
        plt.xlabel("t-SNE Dimension 1")
        plt.ylabel("t-SNE Dimension 2")
        plt.show()

    # 3D Visualization
    elif n_components == 3:
        fig = plt.figure(figsize=(10, 8))
        ax = fig.add_subplot(111, projection='3d')
        ax.scatter(reduced_embeddings[:, 0], reduced_embeddings[:, 1], reduced_embeddings[:, 2])
        ax.set_title("3D t-SNE Visualization of Embeddings")
        ax.set_xlabel("t-SNE Dimension 1")
        ax.set_ylabel("t-SNE Dimension 2")
        ax.set_zlabel("t-SNE Dimension 3")
        plt.show()
    else:
        raise ValueError("n_components must be 2 or 3 for visualization.")


visualize_embeddings(embeddings)

print(time.time() - start)

# Alarm
os.system('play -nq -t alsa synth {} sine {}'.format(1, 999))
