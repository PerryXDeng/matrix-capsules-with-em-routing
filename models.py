"""
License: Apache 2.0
Author: Ashley Gritzman
E-mail: ashley.gritzman@za.ibm.com
"""

# Public modules
import tensorflow as tf
import tensorflow.contrib.slim as slim
import numpy as np

# My modules
from config import FLAGS
import utils as utl
import layers as lyr
import em_routing as em

# Get logger that has already been created in config.py
import daiquiri
logger = daiquiri.getLogger(__name__)


#------------------------------------------------------------------------------
# CAPSNET FOR SMALLNORB
#------------------------------------------------------------------------------
def build_arch_smallnorb(inp, is_train: bool, num_classes: int, y=None):
  inp_shape = inp.get_shape() 
  logger.info('input shape: {}'.format(inp_shape))
  batch_size = FLAGS.batch_size//FLAGS.num_gpus
  offset = 1
  if len(inp_shape.as_list()) == 3:
    offset = 0
  inp.set_shape([batch_size] + inp_shape[offset:].as_list())
  spatial_size = int(inp.get_shape()[1])

  # xavier initialization is necessary here to provide higher stability
  # initializer = tf.truncated_normal_initializer(mean=0.0, stddev=0.01)
  # instead of initializing bias with constant 0, a truncated normal 
  # initializer is exploited here for higher stability
  bias_initializer = tf.truncated_normal_initializer(mean=0.0, stddev=0.01) 

  # AG 13/11/2018
  # In response to a question on OpenReview, Hinton et al. wrote the 
  # following:
  # "We use a weight decay loss with a small factor of .0000002 rather than 
  # the reconstruction loss."
  # https://openreview.net/forum?id=HJWLfGWRb&noteId=rJeQnSsE3X
  nn_weights_regularizer = tf.contrib.layers.l2_regularizer(FLAGS.nn_weight_reg_lambda)
  capsule_weights_regularizer = tf.contrib.layers.l2_regularizer(FLAGS.capsule_weight_reg_lambda)

  # for drop connect during em routing
  drop_rate = FLAGS.drop_rate if is_train else 0

  # weights_initializer=initializer,
  with slim.arg_scope([slim.conv2d, slim.fully_connected], 
    trainable = is_train, 
    biases_initializer = bias_initializer,
    weights_regularizer = nn_weights_regularizer):
    
    #----- Batch Norm -----#
    output = slim.batch_norm(
        inp,
        center=False, 
        is_training=is_train, 
        trainable=is_train)
    
    #----- Convolutional Layer 1 -----#
    with tf.variable_scope('relu_conv1') as scope:
      output = slim.conv2d(output, 
      num_outputs=FLAGS.A, 
      kernel_size=[5, 5], 
      stride=2, 
      padding='SAME', 
      scope=scope, 
      activation_fn=tf.nn.relu)
      
      spatial_size = int(output.get_shape()[1])
      logger.info('relu_conv1 output shape: {}'.format(output.get_shape()))
      assert output.get_shape() == [batch_size, spatial_size, spatial_size, 
                                    FLAGS.A]
    
    #----- Primary Capsules -----#
    with tf.variable_scope('primary_caps') as scope:
      pose = slim.conv2d(output, 
      num_outputs=FLAGS.B * 16, 
      kernel_size=[1, 1], 
      stride=1, 
      padding='VALID', 
      scope='pose', 
      activation_fn=None)
      activation = slim.conv2d(
          output, 
          num_outputs=FLAGS.B, 
          kernel_size=[1, 1], 
          stride=1,
          padding='VALID', 
          scope='activation', 
          activation_fn=tf.nn.sigmoid)

      spatial_size = int(pose.get_shape()[1])
      pose = tf.reshape(pose, shape=[batch_size, spatial_size, spatial_size, 
                                     FLAGS.B, 16], name='pose')
      activation = tf.reshape(
          activation, 
          shape=[batch_size, spatial_size, spatial_size, FLAGS.B, 1], 
          name="activation")
      
      logger.info('primary_caps pose shape: {}'.format(pose.get_shape()))
      logger.info('primary_caps activation shape {}'
                  .format(activation.get_shape()))
      assert pose.get_shape() == [batch_size, spatial_size, spatial_size, 
                                  FLAGS.B, 16]
      assert activation.get_shape() == [batch_size, spatial_size, spatial_size,
                                        FLAGS.B, 1]
      
      tf.summary.histogram("activation", activation)
       
    #----- Conv Caps 1 -----#
    activation, pose = lyr.conv_caps(
        activation_in = activation,
        pose_in = pose,
        kernel = 3, 
        stride = 2,
        ncaps_out = FLAGS.C,
        name = 'lyr.conv_caps1',
        weights_regularizer = capsule_weights_regularizer,
        drop_rate = FLAGS.drop_rate,
        dropout = FLAGS.dropout_extra if is_train else False,
        affine_voting = FLAGS.affine_voting)
    
    #----- Conv Caps 2 -----#
    activation, pose = lyr.conv_caps(
        activation_in = activation, 
        pose_in = pose, 
        kernel = 3, 
        stride = 1, 
        ncaps_out = FLAGS.D, 
        name = 'lyr.conv_caps2',
        weights_regularizer = capsule_weights_regularizer,
        drop_rate = FLAGS.drop_rate,
        dropout = FLAGS.dropout if is_train else False,
        dropconnect = FLAGS.dropconnect if is_train else False,
        affine_voting = FLAGS.affine_voting)

    #----- Conv Caps 3 -----#
    # not part of Hintin's architecture
    if FLAGS.E > 0:
      activation, pose = lyr.conv_caps(
          activation_in = activation,
          pose_in = pose,
          kernel = 3,
          stride = 1,
          ncaps_out = FLAGS.E,
          name = 'lyr.conv_caps3',
          dropout = FLAGS.dropout_extra if is_train else False,
          weights_regularizer = capsule_weights_regularizer,
          affine_voting = FLAGS.affine_voting)
    
    #----- Conv Caps 4 -----#
    if FLAGS.F > 0:
      activation, pose = lyr.conv_caps(
          activation_in = activation, 
          pose_in = pose,
          kernel = 3,
          stride = 1,
          ncaps_out = FLAGS.F, 
          name = 'lyr.conv_caps4',
          weights_regularizer = capsule_weights_regularizer,
          dropout = FLAGS.dropout if is_train else False,
          share_class_kernel=False,
          affine_voting = FLAGS.affine_voting)
    
    #----- Class Caps -----#
    class_activation_out, class_pose_out = lyr.fc_caps(
        activation_in = activation,
        pose_in = pose,
        ncaps_out = num_classes,
        name = 'class_caps',
        weights_regularizer = capsule_weights_regularizer,
        drop_rate = FLAGS.drop_rate,
        dropout = False,
        dropconnect = FLAGS.dropconnect if is_train else False,
        affine_voting = FLAGS.affine_voting)
    act_shape = class_activation_out.get_shape() 
    offset = 1
    if len(act_shape.as_list()) == 1:
      offset = 0
    class_activation_out = tf.reshape(class_activation_out, [batch_size] + act_shape[offset:].as_list())
    class_pose_out = tf.reshape(class_pose_out, [batch_size] + act_shape[offset:].as_list() + [16])
 
    if FLAGS.recon_loss:
      if FLAGS.relu_recon:
        recon_fn = tf.nn.relu
      else:
        recon_fn = tf.nn.tanh
      if not FLAGS.new_bg_recon_arch:
        if FLAGS.multi_weighted_pred_recon:
          class_input = tf.multiply(class_pose_out, tf.expand_dims(class_activation_out, -1))
          dim = int(np.prod(class_input.get_shape()[1:]))
          class_input = tf.reshape(class_input, [batch_size, dim])
        else:
          if y is None:
            selected_classes = tf.argmax(class_activation_out, axis=-1,
                                         name="class_predictions")
          else:
            selected_classes = y
          recon_mask = tf.one_hot(selected_classes, depth=num_classes,
                                  on_value=True, off_value=False, dtype=tf.bool,
                                  name="reconstruction_mask")
          # dim(class_input) = [batch, matrix_size]
          class_input = tf.boolean_mask(class_pose_out, recon_mask, name="masked_pose")
        if FLAGS.num_bg_classes > 0:
          bg_activation, bg_pose = lyr.fc_caps(
            activation_in=activation,
            pose_in=pose,
            ncaps_out=FLAGS.num_bg_classes,
            name='bg_caps',
            weights_regularizer=capsule_weights_regularizer,
            drop_rate=FLAGS.drop_rate,
            dropout=False,
            dropconnect=FLAGS.dropconnect if is_train else False,
            affine_voting=FLAGS.affine_voting)
          act_shape = bg_activation.get_shape()
          bg_activation = tf.reshape(bg_activation, [batch_size] + act_shape[offset:].as_list())
          bg_pose = tf.reshape(bg_pose, [batch_size] + act_shape[offset:].as_list() + [16])

          weighted_bg = tf.multiply(bg_pose, tf.expand_dims(bg_activation, -1))
          bg_size = int(np.prod(weighted_bg.get_shape()[1:]))
          flattened_bg = tf.reshape(weighted_bg, [batch_size, bg_size])
          decoder_input = tf.concat([flattened_bg, class_input], 1)
        else:
          decoder_input = class_input
        output_size = int(np.prod(inp.get_shape()[1:]))
        recon = slim.fully_connected(decoder_input, FLAGS.X,
                                     activation_fn=recon_fn,
                                     scope="recon_1")
        if FLAGS.Y > 0:
          recon = slim.fully_connected(recon, FLAGS.Y,
                                       activation_fn=recon_fn,
                                       scope="recon_2")
        decoder_output = slim.fully_connected(recon, output_size,
                                              activation_fn=tf.nn.sigmoid,
                                              scope="decoder_output")
        out_dict = {'scores': class_activation_out, 'pose_out': class_pose_out,
                    'decoder_out': decoder_output, 'input': inp}
        if FLAGS.zeroed_bg_reconstruction:
          scope.reuse_variables()
          zeroed_bg_decoder_input = tf.concat([tf.zeros(flattened_bg.get_shape()), class_input], 1)
          recon = slim.fully_connected(zeroed_bg_decoder_input, FLAGS.X,
                                       activation_fn=recon_fn,
                                       scope="recon_1")
          if FLAGS.Y > 0:
            recon = slim.fully_connected(recon, FLAGS.Y,
                                         activation_fn=recon_fn,
                                         scope="recon_2")
          zeroed_bg_decoder_output = slim.fully_connected(recon, output_size,
                                                activation_fn=tf.nn.sigmoid,
                                                scope="decoder_output")
          out_dict['zeroed_bg_decoder_out'] = zeroed_bg_decoder_output
        return out_dict
      else:
        if FLAGS.multi_weighted_pred_recon:
          act_shape = class_activation_out.get_shape()
          class_activation_flattened = tf.reshape(class_activation_out, [batch_size] + act_shape[offset:].as_list())
          class_pose_flattened = tf.reshape(class_pose_out, [batch_size] + np.prod(act_shape[offset:].as_list()) * 16)
          class_input = tf.concat(class_activation_flattened, class_pose_flattened)
        else:
          if y is None:
            selected_classes = tf.argmax(class_activation_out, axis=-1,
                                         name="class_predictions")
          else:
            selected_classes = y
          recon_mask = tf.one_hot(selected_classes, depth=num_classes,
                                  on_value=True, off_value=False, dtype=tf.bool,
                                  name="reconstruction_mask")
          # dim(class_input) = [batch, matrix_size]
          class_input = tf.boolean_mask(class_pose_out, recon_mask, name="masked_pose")
        output_size = int(np.prod(inp.get_shape()[1:]))
        class_recon = slim.fully_connected(class_input, FLAGS.X,
                                     activation_fn=recon_fn,
                                     scope="class_recon_1")
        if FLAGS.Y > 0:
          class_recon = slim.fully_connected(class_recon, FLAGS.Y,
                                       activation_fn=recon_fn,
                                       scope="class_recon_2")
        class_output = slim.fully_connected(class_recon, output_size,
                                              activation_fn=tf.nn.sigmoid,
                                              scope="class_output")
        decoder_output = class_output
        out_dict = {'scores': class_activation_out, 'pose_out': class_pose_out,
                    'decoder_out': decoder_output, 'input': inp}
        if FLAGS.num_bg_classes > 0:
          bg_activation, bg_pose = lyr.fc_caps(
            activation_in=activation,
            pose_in=pose,
            ncaps_out=FLAGS.num_bg_classes,
            name='bg_caps',
            weights_regularizer=capsule_weights_regularizer,
            drop_rate=FLAGS.drop_rate,
            dropout=False,
            dropconnect=FLAGS.dropconnect if is_train else False,
            affine_voting=FLAGS.affine_voting)
          act_shape = bg_activation.get_shape()
          bg_activation_flattened = tf.reshape(bg_activation, [batch_size] + act_shape[offset:].as_list())
          bg_pose_flattened = tf.reshape(bg_pose, [batch_size] + np.prod(act_shape[offset:].as_list()) * 16)
          bg_input = tf.concat(bg_activation_flattened, bg_pose_flattened)
          bg_recon = slim.fully_connected(bg_input, FLAGS.X,
                                             activation_fn=recon_fn,
                                             scope="bg_recon_1")
          if FLAGS.Y > 0:
            bg_recon = slim.fully_connected(bg_recon, FLAGS.Y,
                                               activation_fn=recon_fn,
                                               scope="bg_recon_2")
          bg_output = slim.fully_connected(bg_recon, output_size,
                                              activation_fn=tf.nn.sigmoid,
                                              scope="bg_output")
          out_dict['class_out'] = class_output
          decoder_output = class_output + bg_output
          out_dict['decoder_out'] = decoder_output
          out_dict['bg_out'] = bg_output
          if FLAGS.zeroed_bg_reconstruction:
            out_dict['zeroed_bg_decoder_out'] = class_output
        return out_dict
  return {'scores': class_activation_out, 'pose_out': class_pose_out}


def  build_arch_alexnet_modified(inp, is_train: bool, num_classes: int, y=None):
  inp = tf.image.resize(inp, [224, 224])
  scope='alexnet_v2'
  weight_decay = 0.0005
  with tf.compat.v1.variable_scope(scope, 'alexnet', [inp]) as sc:
    with slim.arg_scope([slim.conv2d], padding='SAME'):
      with slim.arg_scope([slim.max_pool2d], padding='VALID') as arg_sc:
        with slim.arg_scope([slim.conv2d, slim.fully_connected],
                          activation_fn=tf.nn.relu,
                          weights_initializer=tf.compat.v1.truncated_normal_initializer(0.0, 0.005),
                          biases_initializer=tf.compat.v1.constant_initializer(0.1),
                          weights_regularizer=slim.l2_regularizer(weight_decay)):
          net = slim.conv2d(inp, 64, [11, 11], 4, padding='VALID',
                            scope='conv1')
          net = slim.max_pool2d(net, [3, 3], 2, scope='pool1')
          net = slim.conv2d(net, 192, [5, 5], scope='conv2')
          net = slim.max_pool2d(net, [3, 3], 2, scope='pool2')
          net = slim.conv2d(net, 384, [3, 3], scope='conv3')
          net = slim.conv2d(net, 384, [3, 3], scope='conv4')
          net = slim.conv2d(net, 256, [3, 3], scope='conv5')
          net = slim.max_pool2d(net, [3, 3], 2, scope='pool5')
          net = slim.flatten(net)
          net = slim.fully_connected(net, 4096, scope='fc6')
          net = slim.dropout(net, 0.5, is_training=is_train, scope='dropout6')
          net = slim.fully_connected(net, 4096, scope='fc7')
          net = slim.dropout(net, 0.5, is_training=is_train, scope='dropout7')
          class_vectors = [slim.fully_connected(net, 16, scope='class_vector_%i'%class_num)
                           for class_num in range(num_classes)]
          class_logits = [slim.fully_connected(class_vectors[class_num], 1, activation_fn=None,
                                               scope='fc8_%i'%class_num) for class_num in range(num_classes)]
          class_vectors = tf.stack(class_vectors, axis=1)
          class_logits = tf.concat(class_logits, axis=1)
  if FLAGS.recon_loss:
    if FLAGS.relu_recon:
      recon_fn = tf.nn.relu
    else:
      recon_fn = tf.nn.tanh
    if y is None:
      selected_classes = tf.argmax(class_logits, axis=-1,
                                   name="class_predictions")
    else:
      selected_classes = y
    recon_mask = tf.one_hot(selected_classes, depth=num_classes,
                            on_value=True, off_value=False, dtype=tf.bool,
                            name="reconstruction_mask")
    # dim(class_input) = [batch, matrix_size]
    class_input = tf.boolean_mask(class_vectors, recon_mask, name="masked_pose")
    output_size = int(np.prod(inp.get_shape()[1:]))
    class_recon = slim.fully_connected(class_input, FLAGS.X,
                                       activation_fn=recon_fn,
                                       scope="class_recon_1")
    if FLAGS.Y > 0:
      class_recon = slim.fully_connected(class_recon, FLAGS.Y,
                                         activation_fn=recon_fn,
                                         scope="class_recon_2")
    class_output = slim.fully_connected(class_recon, output_size,
                                        activation_fn=tf.nn.sigmoid,
                                        scope="class_output")
    decoder_output = class_output
    out_dict = {'scores': class_logits, 'pose_out': class_vectors,
                'decoder_out': decoder_output, 'input': inp}
    return out_dict
  return {'scores': class_logits, 'pose_out': class_vectors}


#------------------------------------------------------------------------------
# LOSS FUNCTIONS
#------------------------------------------------------------------------------
def spread_loss(scores, y):
  """Spread loss.
  
  "In order to make the training less sensitive to the initialization and 
  hyper-parameters of the model, we use “spread loss” to directly maximize the 
  gap between the activation of the target class (a_t) and the activation of the 
  other classes. If the activation of a wrong class, a_i, is closer than the 
  margin, m, to at then it is penalized by the squared distance to the margin."
  
  See Hinton et al. "Matrix Capsules with EM Routing" equation (3).
  
  Author:
    Ashley Gritzman 19/10/2018  
  Credit:
    Adapted from Suofei Zhang's implementation on GitHub, "Matrix-Capsules-
    EM-Tensorflow"
    https://github.com/www0wwwjs1/Matrix-Capsules-EM-Tensorflow  
  Args: 
    scores: 
      scores for each class 
      (batch_size, num_class)
    y: 
      index of true class 
      (batch_size, 1)  
  Returns:
    loss: 
      mean loss for entire batch
      (scalar)
  """
  
  with tf.variable_scope('spread_loss') as scope:
    batch_size = int(scores.get_shape()[0])

    # AG 17/09/2018: modified margin schedule based on response of authors to 
    # questions on OpenReview.net: 
    # https://openreview.net/forum?id=HJWLfGWRb
    # "The margin that we set is: 
    # margin = 0.2 + .79 * tf.sigmoid(tf.minimum(10.0, step / 50000.0 - 4))
    # where step is the training step. We trained with batch size of 64."
    global_step = tf.to_float(tf.train.get_global_step())
    m_min = 0.2
    m_delta = 0.79
    m = (m_min 
         + m_delta * tf.sigmoid(tf.minimum(10.0, global_step / 50000.0 - 4)))

    num_class = int(scores.get_shape()[-1])

    y = tf.one_hot(y, num_class, dtype=tf.float32)
    
    # Get the score of the target class
    # (64, 1, 5)
    scores = tf.reshape(scores, shape=[batch_size, 1, num_class])
    # (64, 5, 1)
    y = tf.expand_dims(y, axis=2)
    # (64, 1, 5)*(64, 5, 1) = (64, 1, 1)
    at = tf.matmul(scores, y)
    
    # Compute spread loss, paper eq (3)
    loss = tf.square(tf.maximum(0., m - (at - scores)))
    
    # Sum losses for all classes
    # (64, 1, 5)*(64, 5, 1) = (64, 1, 1)
    # e.g loss*[1 0 1 1 1]
    loss = tf.matmul(loss, 1. - y)
    
    # Compute mean
    loss = tf.reduce_mean(loss)

  return loss


def cross_ent_loss(logits, y):
  """Cross entropy loss.
  
  Author:
    Ashley Gritzman 06/05/2019  
  Args: 
    logits: 
      logits for each class 
      (batch_size, num_class)
    y: 
      index of true class 
      (batch_size, 1)  
  Returns:
    loss: 
      mean loss for entire batch
      (scalar)
  """
  loss = tf.losses.sparse_softmax_cross_entropy(labels=y, logits=logits)
  loss = tf.reduce_mean(loss)

  return loss


def reconstruction_loss(input_images, decoder_output, batch_reduce=True):
  with tf.variable_scope('reconstruction_loss') as scope:
    output_size = int(np.prod(input_images.get_shape()[1:]))
    flat_images = tf.reshape(input_images, [-1, output_size])
    sqrd_diff = tf.square(flat_images - decoder_output)
    if batch_reduce:
      recon_loss = tf.reduce_mean(sqrd_diff)
    else:
      recon_loss = tf.reduce_mean(sqrd_diff, axis=-1)
  return recon_loss

 
def total_loss(output, y):
  """total_loss = spread_loss/cross_entropy_loss + regularization_loss.
  
  If the flag to regularize is set, the the total loss is the sum of the spread   loss and the regularization loss.
  
  Author:
    Ashley Gritzman 19/10/2018  
  Credit:
    Adapted from Suofei Zhang's implementation on GitHub, "Matrix-Capsules-
    EM-Tensorflow"
    https://github.com/www0wwwjs1/Matrix-Capsules-EM-Tensorflow  
  Args: 
    scores: 
      scores for each class 
      (batch_size, num_class)
    y: 
      index of true class 
      (batch_size, 1)  
  Returns:
    total_loss: 
      mean total loss for entire batch
      (scalar)
  """
  with tf.variable_scope('total_loss') as scope:
    # classification loss
    scores = output["scores"]
    if FLAGS.cnn:
      total = cross_ent_loss(y=y, logits=scores)
    else:
      total = spread_loss(scores, y)
    tf.summary.scalar('spread_loss', total)

    if FLAGS.weight_reg:
      # Regularization
      regularization_losses = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
      if regularization_losses:
        reg_loss = tf.add_n(regularization_losses)
      else:
        print("NO REGULARIZED VARIABLE IN GRAPH")
        reg_loss = tf.constant(0.0)
      total += reg_loss
      tf.summary.scalar('regularization_loss', reg_loss)
    
    if FLAGS.recon_loss:
      # Capsule Reconstruction
      x = output["input"]
      decoder_output = output["decoder_out"]
      recon_loss = FLAGS.recon_loss_lambda * reconstruction_loss(x,
                                                 decoder_output)
      total += recon_loss
      tf.summary.scalar('reconstruction_loss', recon_loss)
      if FLAGS.new_bg_recon_arch and FLAGS.num_bg_classes > 0:
        class_bg_distance_loss = -1 * FLAGS.recon_diff_lambda\
                                 * tf.reduce_mean(tf.square(output["class_out"] - output["bg_out"]))
        total += class_bg_distance_loss
        tf.summary.scalar('class_bg_distance_loss', class_bg_distance_loss)
  return total


def carlini_wagner_loss(output, y, num_classes):
  # the cost function from Towards Evaluating the Robustness of Neural Networks
  # without the pertubation norm which does not apply to adversarial patching
  with tf.variable_scope('carlini_wagner_loss') as scope:
    logits = output["scores"]
    target_mask = tf.one_hot(y, depth=num_classes,
                             on_value=True, off_value=False, dtype=tf.bool,
                             name="target_mask")
    non_target_mask= tf.logical_not(target_mask, name="non_target_mask")
    target_logits = tf.boolean_mask(logits, target_mask, name="target_logits")
    non_target_logits = tf.boolean_mask(logits, non_target_mask, name="non_target_logits")
    max_non_target_logits = tf.reduce_max(non_target_logits, axis=-1,
                                          name="max_non_target_logits")
    adversarial_confidence = max_non_target_logits - target_logits
    confidence_lowerbound = tf.fill(logits.get_shape()[0:-1],
                                    FLAGS.adv_conf_thres * -1,
                                    name="adversarial_confidence_lowerbound")
    total_loss = tf.reduce_mean(tf.maximum(adversarial_confidence, confidence_lowerbound),
                                name="CW_loss")
    tf.summary.scalar('carlini_wagner_loss', total_loss)

    if FLAGS.recon_loss:
      # Capsule Reconstruction
      x = output["input"]
      decoder_output = output["decoder_out"]
      recon_loss = FLAGS.recon_loss_lambda * reconstruction_loss(x,
                                                 decoder_output)
      total_loss += recon_loss
      tf.summary.scalar('reconstruction_loss', recon_loss)
  return total_loss

