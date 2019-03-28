# -*- coding: utf-8 -*-

import numpy as np

from ..base import Property
from .base import Updater
from ..types import GaussianMeasurementPrediction, GaussianStateUpdate, CovarianceMatrix
from ..models import LinearGaussian
from ..models.measurement import MeasurementModel
from ..functions import gauss2sigma, unscented_transform


class KalmanUpdater(Updater):
    """
    An class which embodies Kalman-type updaters; also a class which performs measurement update step as in the
    standard Kalman Filter. Therefore assumes the measurement matrix function of the measurement_model returns a
    matrix (:math:`H_k`). Daughter classes can overwrite to specify the measurement model :math:`h(\mathbf{x})`.
    The observation model assumes

    .. math::

        \mathbf{z} = h_k(\mathbf{x}) + \sigma_k

    with the specific case of the Kalman updater having :math:`h_k(\mathbf{x}) = H_k \mathbf{x}` and
    :math:`\sigma_k = \mathcal{N}(0,R_k)`.

    """

    # TODO at present this will throw an error if a measurement model is not specified in either individual
    # TODO measurements or the Updater object
    measurement_model = Property(LinearGaussian, default=None, doc="A linear Gaussian measurement model. This need not "
                                                                   "be defined if a measurement model is provided in "
                                                                   "the measurement. If no model specified on "
                                                                   "construction, or in the measurement, then error "
                                                                   "will be thrown.")

    def measurement_matrix(self, predicted_state=None, measurement_model=None, **kwargs):
        """
        This is straightforward Kalman so just get the Matrix from the measurement model

        :return: the measurement matrix, :math:`H_k`
        """
        return self.measurement_model.matrix()

    def predict_measurement(self, predicted_state, measurement_model=None, **kwargs):
        """
        Predict the mean measurement implied by the predicted state

        :param predicted_state: The predicted state :math:`\mathbf{x}_{k|k-1}`
        :param measurement_model: The measurement model. If omitted the model in the updater object is used
        :param kwargs:
        :return: A Gaussian measurement prediction, :math:`\mathbf{z}_{k|k-1}`
        """
        # If a measurement model is not specified then use the one that's native to the updater
        if measurement_model is None:
            measurement_model = self.measurement_model

        pred_meas = measurement_model.function(predicted_state.state_vector, noise=[0])

        hh = self.measurement_matrix(predicted_state=predicted_state, measurement_model=measurement_model)

        innov_cov = hh @ predicted_state.covar @ hh.T + measurement_model.covar()
        meas_cross_cov = predicted_state.covar @ hh.T

        return GaussianMeasurementPrediction(pred_meas, innov_cov, predicted_state.timestamp,
                                             cross_covar=meas_cross_cov)

    def update(self, hypothesis, **kwargs):
        """
        The Kalman update method

        :param hypothesis: the predicted measurement-measurement association hypothesis
        :param kwargs:
        :return: Posterior state, :math:`\mathbf{x}_{k|k}` as :class:`~.GaussianMeasurementPrediction`
        """

        predicted_state = hypothesis.prediction  # Get the predicted state out of the hypothesis

        # Get the measurement model out of the measurement if it's there. If not, use the one native to the updater
        # (which might still be none)
        measurement_model = hypothesis.measurement.measurement_model
        if measurement_model is None:
            measurement_model = self.measurement_model

        # Get the measurement prediction
        gmp = self.predict_measurement(predicted_state, measurement_model=measurement_model, **kwargs)
        pred_meas = gmp.state_vector
        innov_cov = gmp.covar
        m_cross_cov = gmp.cross_covar

        # Complete the calculation of the posterior
        kalman_gain = m_cross_cov @ np.linalg.inv(innov_cov)  # This isn't optimised
        posterior_mean = predicted_state.state_vector + kalman_gain @ (hypothesis.measurement.state_vector - pred_meas)
        posterior_covariance = predicted_state.covar - kalman_gain @ innov_cov @ kalman_gain.T

        # posterior_state_covariance = (P_post + P_post.T) / 2 # !!! kludge

        return GaussianStateUpdate(posterior_mean, posterior_covariance, hypothesis, hypothesis.measurement.timestamp)


class ExtendedKalmanUpdater(KalmanUpdater):
    """
    The EKF version of the Kalman Updater

    The measurement model may be non-linear but must be differentiable and return the linearisation of
    :math:`h(\mathbf{x})` via the matrix :math:`H` accessible via the :attr:`jacobian()` function.

    """
    # TODO Enforce the fact that this version of MeasurementModel must be capable of executing :attr:`jacobian()`
    measurement_model = Property(MeasurementModel, default=None, doc="A measurement model. This need not be defined, "
                                                                     "if a measurement model is provided in the "
                                                                     "measurement. If no model specified on "
                                                                     "construction, or in the measurement, then error "
                                                                     "will be thrown. Must possess :attr:`jacobian()` "
                                                                     "function.")

    def measurement_matrix(self, predicted_state, measurement_model=None, **kwargs):
        """
        Return the (approximate via :attr:`jacobian()`) measurement matrix

        :param predicted_state: the predicted state, :math:`\mathbf{x}_{k|k-1}`
        :param measurement_model: the measurement model. If :attr:`None` defaults to the model defined in updater
        :return: the measurement matrix, :math:`H_k`
        """

        if measurement_model is None:
            measurement_model = self.measurement_model

        if hasattr(measurement_model, 'matrix'):
            return measurement_model.matrix()
        else:
            return measurement_model.jacobian(predicted_state.state_vector)


class UnscentedKalmanUpdater(KalmanUpdater):
    """Unscented Kalman Updater

    Perform measurement update step in the Unscented Kalman Filter. The :attr:`predict_measurement` function uses the
    unscented transform to estimate a Gauss-distributed predicted measurement. This is then updated via the standard
    Kalman update equations.
    """
    # Can be linear and non-differentiable
    measurement_model = Property(MeasurementModel, default=None, doc="The measurement model to be used. This need not "
                                                                      "be defined, if a measurement model is provided "
                                                                      "in the measurement. If no model specified on "
                                                                      "construction, or in the measurement, then error "
                                                                      "will be thrown." )

    alpha = Property(float, default=0.5,
                     doc="Primary sigma point spread scaling parameter.\
                         Typically 1e-3.")
    beta = Property(float, default=2,
                    doc="Used to incorporate prior knowledge of the distribution.\
                        If the true distribution is Gaussian, the value of 2\
                        is optimal.")
    kappa = Property(float, default=0,
                     doc="Secondary spread scaling parameter\
                        (default is calculated as 3-Ns)")

    def predict_measurement(self, predicted_state, measurement_model=None, **kwargs):

        """Unscented Kalman Filter measurement prediction step

        Parameters
        ----------
        predicted_state : :class:`~.GaussianStatePrediction`
            A predicted state object
        measurement_model: :class:`~.MeasurementModel`, optional
            The measurement model used to generate the measurement prediction.Should be used in cases where the
            measurement model is dependent on the received measurement (the default is ``None``, in which case the
            updater will use the measurement model specified on initialisation)

        Returns
        -------
        : :class:`~.GaussianMeasurementPrediction`
            The measurement prediction
        """
        # If a measurement model is not specified then use the one that's native to the updater
        if measurement_model is None:
            measurement_model = self.measurement_model

        sigma_points, mean_weights, covar_weights = \
            gauss2sigma(predicted_state.state_vector, predicted_state.covar, self.alpha, self.beta, self.kappa)

        meas_pred_mean, meas_pred_covar, cross_covar, _, _, _ = \
            unscented_transform(sigma_points, mean_weights, covar_weights, measurement_model.function,
                                covar_noise=measurement_model.covar())

        return GaussianMeasurementPrediction(meas_pred_mean, meas_pred_covar, predicted_state.timestamp, cross_covar)
