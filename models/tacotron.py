import numpy as np
import tensorflow as tf
from tensorflow.contrib.rnn import GRUCell, MultiRNNCell, OutputProjectionWrapper, ResidualWrapper
from tensorflow.contrib.seq2seq import BasicDecoder, BahdanauAttention, AttentionWrapper, BahdanauMonotonicAttention, LuongAttention
from text.symbols import symbols
from util.infolog import log
from .helpers import TacoTestHelper, TacoTrainingHelper
from .modules import encoder_cbhg, post_cbhg, p_post_cbhg, prenet, LocationSensitiveAttention, ZoneoutLSTMCell, GmmAttention, BahdanauStepwiseMonotonicAttention
from .rnn_wrappers import DecoderPrenetWrapper, ConcatOutputAndAttentionWrapper


class Tacotron2():
    def __init__(self, hparams):
        self._hparams = hparams

    def initialize(self, c_inputs, p_inputs, c_input_lengths, p_input_lengths, mel_targets=None, linear_targets=None):
        '''Initializes the model for inference.

        Sets "mel_outputs", "linear_outputs", and "alignments" fields.

        Args:
          c_inputs: int32 Tensor with shape [N, T_in] where N is batch size, T_in is number of
            steps in the input time series, and values are character IDs
          p_inputs: int32 Tensor with shape [N, T_in] where N is batch size, T_in is number of
            steps in the input time series, and values are phoneme IDs
          c_input_lengths and p_input_lenghts: int32 Tensor with shape [N] where N is batch size and values are the lengths
            of each sequence in inputs.
          mel_targets: float32 Tensor with shape [N, T_out, M] where N is batch size, T_out is number
            of steps in the output time series, M is num_mels, and values are entries in the mel
            spectrogram. Only needed for training.
          linear_targets: float32 Tensor with shape [N, T_out, F] where N is batch_size, T_out is number
            of steps in the output time series, F is num_freq, and values are entries in the linear
            spectrogram. Only needed for training.
        '''
        with tf.variable_scope('inference') as scope:
            is_training = linear_targets is not None
            batch_size = tf.shape(c_inputs)[0]
            # input_lengths = c_input_lengths+p_input_lengths #for concat character and phoneme
            hp = self._hparams

            # Embeddings
            embedding_table = tf.get_variable(
                'embedding', [len(symbols), hp.embed_depth], dtype=tf.float32,
                initializer=tf.truncated_normal_initializer(stddev=0.5))
            #
            c_embedded_inputs = tf.nn.embedding_lookup(embedding_table, c_inputs)  # [N, c_T_in, embed_depth=256]
            p_embedded_inputs = tf.nn.embedding_lookup(embedding_table, p_inputs)  # [N, p_T_in, embed_depth=256]
        with tf.variable_scope('Encoder') as scope:

            c_x = c_embedded_inputs
            p_x = p_embedded_inputs

            #3 Conv Layers
            for i in range(3):
                c_x = tf.layers.conv1d(c_x,filters=512,kernel_size=5,padding='same',activation=tf.nn.relu,name='c_Encoder_{}'.format(i))
                c_x = tf.layers.batch_normalization(c_x, training=is_training)
                c_x = tf.layers.dropout(c_x, rate=0.5, training=is_training, name='dropout_{}'.format(i))
            c_encoder_conv_output = c_x

            for i in range(3):
                p_x = tf.layers.conv1d(p_x,filters=512,kernel_size=5,padding='same',activation=tf.nn.relu,name='p_Encoder_{}'.format(i))
                p_x = tf.layers.batch_normalization(p_x, training=is_training)
                p_x = tf.layers.dropout(p_x, rate=0.5, training=is_training, name='dropout_{}'.format(i))
            p_encoder_conv_output = p_x
            
            #bi-directional LSTM
            cell_fw= ZoneoutLSTMCell(256, is_training, zoneout_factor_cell=0.1, zoneout_factor_output=0.1, name='encoder_fw_LSTM')
            cell_bw= ZoneoutLSTMCell(256, is_training, zoneout_factor_cell=0.1, zoneout_factor_output=0.1, name='encoder_bw_LSTM')
           
            c_outputs, c_states = tf.nn.bidirectional_dynamic_rnn(cell_fw, cell_bw, c_encoder_conv_output, sequence_length=c_input_lengths, dtype=tf.float32)
            p_outputs, p_states = tf.nn.bidirectional_dynamic_rnn(cell_fw, cell_bw, p_encoder_conv_output, sequence_length=p_input_lengths, dtype=tf.float32)

            # c_envoder_outpust = [N,c_T,2*encoder_lstm_units] = [N,c_T,512]
            c_encoder_outputs = tf.concat(c_outputs, axis=2) # Concat and return forward + backward outputs
            # p_envoder_outpust = [N,p_T,2*encoder_lstm_units] = [N,p_T,512]
            p_encoder_outputs = tf.concat(p_outputs, axis=2)
            # Concat and return character + phoneme = [N, c_T+p_T, 512]
            # encoder_outputs = tf.concat([c_encoder_outputs, p_encoder_outputs], axis=1)

        with tf.variable_scope('Decoder') as scope:
            
            if hp.attention_type == 'loc_sen': # Location Sensitivity Attention
                c_attention_mechanism = LocationSensitiveAttention(128, c_encoder_outputs,hparams=hp, is_training=is_training,
                                    mask_encoder=True, memory_sequence_length = c_input_lengths, smoothing=False, cumulate_weights=True)
            elif hp.attention_type == 'gmm': # GMM Attention
                c_attention_mechanism = GmmAttention(128, memory=c_encoder_outputs, memory_sequence_length = c_input_lengths) 
            elif hp.attention_type == 'step_bah':
                c_attention_mechanism = BahdanauStepwiseMonotonicAttention(128, c_encoder_outputs, memory_sequence_length = c_input_lengths, mode="parallel")
            elif hp.attention_type == 'mon_bah':
                c_attention_mechanism = BahdanauMonotonicAttention(128, c_encoder_outputs, memory_sequence_length = c_input_lengths, normalize=True)
            elif hp.attention_type == 'loung':
                c_attention_mechanism = LuongAttention(128, c_encoder_outputs, memory_sequence_length = c_input_lengths) 

            if hp.attention_type == 'loc_sen': # Location Sensitivity Attention
                p_attention_mechanism = LocationSensitiveAttention(128, p_encoder_outputs,hparams=hp, is_training=is_training,
                                    mask_encoder=True, memory_sequence_length = p_input_lengths, smoothing=False, cumulate_weights=True)
            elif hp.attention_type == 'gmm': # GMM Attention
                p_attention_mechanism = GmmAttention(128, memory=p_encoder_outputs, memory_sequence_length = p_input_lengths) 
            elif hp.attention_type == 'step_bah':
                p_attention_mechanism = BahdanauStepwiseMonotonicAttention(128, p_encoder_outputs, memory_sequence_length = p_input_lengths, mode="parallel")
            elif hp.attention_type == 'mon_bah':
                p_attention_mechanism = BahdanauMonotonicAttention(128, p_encoder_outputs, memory_sequence_length = p_input_lengths, normalize=True)
            elif hp.attention_type == 'loung':
                p_attention_mechanism = LuongAttention(128, p_encoder_outputs, memory_sequence_length = p_input_lengths) 

            # attention_mechanism = LocationSensitiveAttention(128, encoder_outputs, hparams=hp, is_training=is_training, mask_encoder=True, memory_sequence_length = input_lengths, smoothing=False, cumulate_weights=True)
            #mask_encoder: whether to mask encoder padding while computing location sensitive attention. Set to True for better prosody but slower convergence.
            #cumulate_weights: Whether to cumulate (sum) all previous attention weights or simply feed previous weights (Recommended: True)
            
            decoder_lstm = [ZoneoutLSTMCell(1024, is_training, zoneout_factor_cell=0.1, zoneout_factor_output=0.1, name='decoder_LSTM_{}'.format(i+1)) for i in range(2)]
            
            decoder_lstm = tf.contrib.rnn.MultiRNNCell(decoder_lstm, state_is_tuple=True)
            # decoder_init_state = decoder_lstm.zero_state(batch_size=batch_size, dtype=tf.float32) #tensorflow1에는 없음
            
            c_attention_cell = AttentionWrapper(decoder_lstm, c_attention_mechanism, alignment_history=True, output_attention=False)
            p_attention_cell = AttentionWrapper(decoder_lstm, p_attention_mechanism, alignment_history=True, output_attention=False)

            # attention_state_size = 256
            # Decoder input -> prenet -> decoder_lstm -> concat[output, attention]
            c_dec_outputs = DecoderPrenetWrapper(c_attention_cell, is_training, hp.prenet_depths)
            c_dec_outputs_cell = OutputProjectionWrapper(c_dec_outputs,(hp.num_mels) * hp.outputs_per_step)

            p_dec_outputs = DecoderPrenetWrapper(p_attention_cell, is_training, hp.prenet_depths)
            p_dec_outputs_cell = OutputProjectionWrapper(p_dec_outputs,(hp.num_mels) * hp.outputs_per_step)

            if is_training:
                helper = TacoTrainingHelper(c_inputs, p_inputs, mel_targets, hp.num_mels, hp.outputs_per_step)
            else:
                helper = TacoTestHelper(batch_size, hp.num_mels, hp.outputs_per_step)
                
            c_decoder_init_state = c_dec_outputs_cell.zero_state(batch_size=batch_size, dtype=tf.float32)
            (c_decoder_outputs, _), c_final_decoder_state, _ = tf.contrib.seq2seq.dynamic_decode(
                BasicDecoder(c_dec_outputs_cell, helper, c_decoder_init_state),
                maximum_iterations=hp.max_iters)  # [N, T_out/r, M*r]

            p_decoder_init_state = p_dec_outputs_cell.zero_state(batch_size=batch_size, dtype=tf.float32)
            (p_decoder_outputs, _), p_final_decoder_state, _ = tf.contrib.seq2seq.dynamic_decode(
                BasicDecoder(p_dec_outputs_cell, helper, p_decoder_init_state),
                maximum_iterations=hp.max_iters)  # [N, T_out/r, M*r]

            # Reshape outputs to be one output per entry
            c_decoder_mel_outputs = tf.reshape(c_decoder_outputs[:,:,:hp.num_mels * hp.outputs_per_step], [batch_size, -1, hp.num_mels])  # [N, T_out, M]
            p_decoder_mel_outputs = tf.reshape(p_decoder_outputs[:,:,:hp.num_mels * hp.outputs_per_step], [batch_size, -1, hp.num_mels])  # [N, T_out, M]
            decoder_mel_outputs = c_decoder_mel_outputs + p_decoder_mel_outputs
            #stop_token_outputs = tf.reshape(decoder_outputs[:,:,hp.num_mels * hp.outputs_per_step:], [batch_size, -1]) # [N,iters]
            
     # Postnet
            x = p_decoder_mel_outputs
            for i in range(5):
                activation = tf.nn.tanh if i != (4) else None
                x = tf.layers.conv1d(x,filters=512, kernel_size=5, padding='same', activation=activation, name='C_Postnet_{}'.format(i))
                x = tf.layers.batch_normalization(x, training=is_training)
                x = tf.layers.dropout(x, rate=0.5, training=is_training, name='C_Postnet_dropout_{}'.format(i))
            
            p_residual = tf.layers.dense(x, hp.num_mels, name='p_residual_projection')
            mel_outputs = c_decoder_mel_outputs + p_residual

            
            # for i in range(5):
            #     activation = tf.nn.tanh if i != (4) else None
            #     p = tf.layers.conv1d(p,filters=512, kernel_size=5, padding='same', activation=activation, name='P_Postnet_{}'.format(i))
            #     p = tf.layers.batch_normalization(p, training=is_training)
            #     p = tf.layers.dropout(p, rate=0.5, training=is_training, name='P_Postnet_dropout_{}'.format(i))
 
            # p_residual = tf.layers.dense(p, hp.num_mels, name='p_residual_projection')
            # p_mel_outputs = p_decoder_mel_outputs + p_residual

            # Add post-processing CBHG:
            # mel_outputs: (N,T,num_mels)
            post_outputs = post_cbhg(mel_outputs, hp.num_mels, is_training, hp.postnet_depth)
            linear_outputs = tf.layers.dense(post_outputs, hp.num_freq)    # [N, T_out, F(1025)]
 
            # p_post_outputs = p_post_cbhg(p_mel_outputs, hp.num_mels, is_training, hp.postnet_depth)
            # p_linear_outputs = tf.layers.dense(p_post_outputs, hp.num_freq)    # [N, T_out, F(1025)]

            # Grab alignments from the final decoder state:
            c_alignments = tf.transpose(c_final_decoder_state.alignment_history.stack(), [1, 2, 0])  # batch_size, text length(encoder), target length(decoder)
            p_alignments = tf.transpose(p_final_decoder_state.alignment_history.stack(), [1, 2, 0])  # batch_size, text length(encoder), target length(decoder)
			
            self.c_inputs = c_inputs
            self.p_inputs = p_inputs
            self.c_input_lengths = c_input_lengths
            self.p_input_lengths = p_input_lengths
            self.decoder_mel_outputs = decoder_mel_outputs
            self.mel_outputs = mel_outputs
            self.linear_outputs = linear_outputs
            # self.p_decoder_mel_outputs = p_decoder_mel_outputs
            # self.p_mel_outputs = p_mel_outputs
            # self.p_linear_outputs = p_linear_outputs
            self.c_alignments = c_alignments
            self.p_alignments = p_alignments
            self.mel_targets = mel_targets
            self.linear_targets = linear_targets
            #self.stop_token_targets = stop_token_targets
            #self.stop_token_outputs = stop_token_outputs
            self.all_vars = tf.trainable_variables()
            log('Initialized Tacotron model. Dimensions: ')
            log('  c_embedding:               %d' % c_embedded_inputs.shape[-1])
            log('  p_embedding:               %d' % p_embedded_inputs.shape[-1])
            # log('  prenet out:              %d' % prenet_outputs.shape[-1])
            log('  encoder out:             %d' % c_encoder_outputs.shape[-1])
            log('  attention out:           %d' % c_attention_cell.output_size)
            #log('  concat attn & out:       %d' % concat_cell.output_size)
            log('  decoder cell out:        %d' % c_dec_outputs_cell.output_size)
            log('  decoder out (%d frames):  %d' % (hp.outputs_per_step, c_decoder_outputs.shape[-1]))
            log('  decoder out (1 frame):   %d' % mel_outputs.shape[-1])
            log('  postnet out:             %d' % post_outputs.shape[-1])
            log('  linear out:              %d' % linear_outputs.shape[-1])

    def add_loss(self):
        '''Adds loss to the model. Sets "loss" field. initialize must have been called.'''
        with tf.variable_scope('loss') as scope:
            hp = self._hparams
            before = tf.losses.mean_squared_error(self.mel_targets, self.decoder_mel_outputs)
            after = tf.losses.mean_squared_error(self.mel_targets, self.mel_outputs)
            # p_before = tf.losses.mean_squared_error(self.mel_targets, self.p_decoder_mel_outputs)
            # p_after = tf.losses.mean_squared_error(self.mel_targets, self.p_mel_outputs) 

            self.mel_loss = before + after


            #self.stop_token_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(labels=self.stop_token_targets, logits=self.stop_token_outputs))

            l1 = tf.abs(self.linear_targets - self.linear_outputs)
            # p_l1 = tf.abs(self.linear_targets - self.p_linear_outputs)
            # Prioritize loss for frequencies under 3000 Hz.
            n_priority_freq = int(3000 / (hp.sample_rate * 0.5) * hp.num_freq)
            self.linear_loss = 0.5 * tf.reduce_mean(l1) + 0.5 * tf.reduce_mean(l1[:, :, 0:n_priority_freq])

            self.regularization = tf.add_n([tf.nn.l2_loss(v) for v in self.all_vars
						if not('bias' in v.name or 'Bias' in v.name or '_projection' in v.name or 'inputs_embedding' in v.name
							or 'RNN' in v.name or 'LSTM' in v.name)]) * hp.reg_weight
            self.loss = self.mel_loss + self.linear_loss + self.regularization

    def add_optimizer(self, global_step):
        '''Adds optimizer. Sets "gradients" and "optimize" fields. add_loss must have been called.

        Args:
          global_step: int32 scalar Tensor representing current global step in training
        '''
        with tf.variable_scope('optimizer') as scope:
            hp = self._hparams
            if hp.decay_learning_rate:
                self.learning_rate = _learning_rate_decay(hp.initial_learning_rate, global_step)
            else:
                self.learning_rate = tf.convert_to_tensor(hp.initial_learning_rate)
            optimizer = tf.train.AdamOptimizer(self.learning_rate, hp.adam_beta1, hp.adam_beta2)
            gradients, variables = zip(*optimizer.compute_gradients(self.loss))
            self.gradients = gradients
            clipped_gradients, _ = tf.clip_by_global_norm(gradients, 1.0)

            # Add dependency on UPDATE_OPS; otherwise batchnorm won't work correctly. See:
            # https://github.com/tensorflow/tensorflow/issues/1122
            with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
                self.optimize = optimizer.apply_gradients(zip(clipped_gradients, variables),
                                                          global_step=global_step)


def _learning_rate_decay(init_lr, global_step):
    # Noam scheme from tensor2tensor:
    warmup_steps = 4000.0
    step = tf.cast(global_step + 1, dtype=tf.float32)
    return init_lr * warmup_steps ** 0.5 * tf.minimum(step * warmup_steps ** -1.5, step ** -0.5)
