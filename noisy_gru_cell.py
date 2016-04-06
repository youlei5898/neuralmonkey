import tensorflow as tf
from tensorflow.models.rnn.rnn_cell import RNNCell, linear
import math

class NoisyGRUCell(RNNCell):
  """
  Gated Recurrent Unit cell (cf. http://arxiv.org/abs/1406.1078) with noisy
  activation functions (http://arxiv.org/abs/1603.00391). It is based on the
  TensorFlow implementatin of GRU just the activation function are changed for
  the noisy ones.
  """

  def __init__(self, num_units, training, input_size=None):
    self._num_units = num_units
    self._input_size = num_units if input_size is None else input_size
    self.training = training

  @property
  def input_size(self):
    return self._input_size

  @property
  def output_size(self):
    return self._num_units

  @property
  def state_size(self):
    return self._num_units

  def __call__(self, inputs, state, scope=None):
    """Gated recurrent unit (GRU) with nunits cells."""
    with tf.variable_scope(scope or type(self).__name__):  # "GRUCell"
      with tf.variable_scope("Gates"):  # Reset gate and update gate.
        # We start with bias of 1.0 to not reset and not update.
        r, u = array_ops.split(1, 2, linear([inputs, state],
                                            2 * self._num_units, True, 1.0))
        r, u = noisy_sigmoid(r), noisy_sigmoid(u)
      with tf.variable_scope("Candidate"):
        c = tanh(linear([inputs, r * state], self._num_units, True))
      new_h = u * state + (1 - u) * c
    return new_h, new_h


def noisy_activation(x, generic, linearized, training, alhpa=1.1):
    """
    Implements the noisy activation with Half-Normal Noise for Hard-Saturation
    functions. See http://arxiv.org/abs/1603.00391, Algorithm 1.

    Args:

        x: Tensor which is an input to the activation function

        generic: The generic formulation of the activation function. (denoted
            as h in the paper)

        linearized: Linearization of the activation based on the first-order
            Tailor expansion around zero. (denoted as u in the paper)

        training: A boolean tensor telling whether we are in the training stage
            (and the noise is sampled) or in runtime when the expactation is
            used instead.

        alpha: Mixing hyper-parameter.

    """

    delta = generic(x) - linerized(x)
    d = -tf.sign(x) * tf.sign(1 - alpha)
    p = tf.Variable(1.0)
    sigma = (tf.sigmoid(p * delta) - 0.5)  ** 2
    noise = tf.select(training, tf.abs(tf.random_normal([1])), math.sqrt(2 / math.pi))
    activation = alpha * generic + (1 - alhpa) * linerized + d * sigma * noise
    return activation


def noisy_sigmoid(x, training, alpha=1.1):
    return noisy_activation(x, tf.sigmoid, lambda y: .25 * y + .5, training, alpha)


def noisy_tanh(x, training, alpha=1.1):
    return noisy_activation(x, tf.tanh, lambda y: y, training, alpha)