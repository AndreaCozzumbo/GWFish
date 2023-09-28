import numpy as np
import GWFish.modules.waveforms as wf
import GWFish.modules.detection as det
import GWFish.modules.auxiliary as aux
import GWFish.modules.fft as fft

import copy
import pandas as pd
from typing import Optional

from tqdm import tqdm

def invertSVD(matrix):
    thresh = 1e-10

    dm = np.sqrt(np.diag(matrix))
    normalizer = np.outer(dm, dm)
    matrix_norm = matrix / normalizer

    [U, S, Vh] = np.linalg.svd(matrix_norm)

    kVal = sum(S > thresh)
    matrix_inverse_norm = U[:, 0:kVal] @ np.diag(1. / S[0:kVal]) @ Vh[0:kVal, :]

    # print(matrix @ (matrix_inverse_norm / normalizer))

    return matrix_inverse_norm / normalizer, S

def fft_derivs_at_detectors(deriv_list, frequency_vector):
    """
    A wrapper for fft_lal_timeseries
    """
    delta_f = frequency_vector[1,0] - frequency_vector[0,0]
    ffd_deriv_list = []
    for deriv in deriv_list:
        ffd_deriv_list.append(fft.fft_lal_timeseries(deriv, delta_f, f_start=0.).data.data)

    # Because f_start = 0 Hz, we need to mask some frequencies
    idx_f_low = int(frequency_vector[0,0]/delta_f)
    idx_f_high = int(frequency_vector[-1,0]/delta_f)

    return np.vstack(ffd_deriv_list).T[idx_f_low:idx_f_high+1,:]

class Derivative:
    """
    Standard GWFish waveform derivative class, based on finite differencing in frequency domain.
    Calculates derivatives with respect to geocent_time, merger phase, and distance analytically.
    Derivatives of other parameters are calculated numerically.

    eps: 1e-5, this follows the simple "cube root of numerical precision" recommendation, which is 1e-16 for double
    """
    def __init__(self, waveform, parameters, detector, eps=1e-5, waveform_class=wf.Waveform):
        self.waveform = waveform
        self.detector = detector
        self.eps = eps
        self.waveform_class = waveform_class
        self.data_params = {'frequencyvector': detector.frequencyvector, 'f_ref': 50.}
        self.waveform_object = waveform_class(waveform, parameters, self.data_params)
        self.waveform_at_parameters = None
        self.projection_at_parameters = None

        # For central parameters and their epsilon-neighbourhood
        self.local_params = parameters.copy()
        self.pv_set1 = parameters.copy()
        self.pv_set2 = parameters.copy()

        self.tc = self.local_params['geocent_time']

    @property
    def waveform_at_parameters(self):
        """
        Return a waveform at the point in parameter space determined by the parameters argument.

        Returns tuple, (wave, t_of_f).
        """
        if self._waveform_at_parameters is None:
            wave = self.waveform_object()
            t_of_f = self.waveform_object.t_of_f
            self._waveform_at_parameters = (wave, t_of_f)
        return self._waveform_at_parameters

    @waveform_at_parameters.setter
    def waveform_at_parameters(self, new_waveform_data):
        self._waveform_at_parameters = new_waveform_data

    @property
    def projection_at_parameters(self):
        if self._projection_at_parameters is None:
            self._projection_at_parameters = det.projection(self.local_params, self.detector,
                                                            self.waveform_at_parameters[0], # wave
                                                            self.waveform_at_parameters[1]) # t(f)
        return self._projection_at_parameters

    @projection_at_parameters.setter
    def projection_at_parameters(self, new_projection_data):
        self._projection_at_parameters = new_projection_data

    def with_respect_to(self, target_parameter):
        """
        Return a derivative with respect to target_parameter at the point in 
        parameter space determined by the argument parameters.
        """
        if target_parameter == 'luminosity_distance':
            derivative = -1. / self.local_params[target_parameter] * self.projection_at_parameters
        elif target_parameter == 'geocent_time':
            derivative = 2j * np.pi * self.detector.frequencyvector * self.projection_at_parameters
        elif target_parameter == 'phase':
            derivative = -1j * self.projection_at_parameters
        else:
            pv = self.local_params[target_parameter]

            dp = np.maximum(self.eps, self.eps * pv)

            self.pv_set1 = self.local_params.copy()
            self.pv_set2 = self.local_params.copy()
            self.pv_set1[target_parameter] = pv - dp / 2.
            self.pv_set2[target_parameter] = pv + dp / 2.

            if target_parameter in ['ra', 'dec', 'psi']:  # these parameters do not influence the waveform
    
                signal1 = det.projection(self.pv_set1, self.detector, 
                                         self.waveform_at_parameters[0], 
                                         self.waveform_at_parameters[1])
                signal2 = det.projection(self.pv_set2, self.detector, 
                                         self.waveform_at_parameters[0], 
                                         self.waveform_at_parameters[1])
    
                derivative = (signal2 - signal1) / dp
            else:
                # to improve precision of numerical differentiation
                self.pv_set1['geocent_time'] = 0.
                self.pv_set2['geocent_time'] = 0.

                waveform_obj1 = self.waveform_class(self.waveform, self.pv_set1, self.data_params)
                wave1 = waveform_obj1()
                t_of_f1 = waveform_obj1.t_of_f

                waveform_obj2 = self.waveform_class(self.waveform, self.pv_set2, self.data_params)
                wave2 = waveform_obj2()
                t_of_f2 = waveform_obj2.t_of_f                

                self.pv_set1['geocent_time'] = self.tc
                self.pv_set2['geocent_time'] = self.tc
                signal1 = det.projection(self.pv_set1, self.detector, wave1, t_of_f1 + self.tc)
                signal2 = det.projection(self.pv_set2, self.detector, wave2, t_of_f2 + self.tc)
    

                derivative = np.exp(2j * np.pi * self.detector.frequencyvector \
                                    * self.tc) * (signal2 - signal1) / dp
                                    
        self.waveform_object.update_gw_params(self.local_params)

        return derivative

    def __call__(self, target_parameter):
        return self.with_respect_to(target_parameter)

class FisherMatrix:
    def __init__(self, waveform, parameters, fisher_parameters, detector, eps=1e-5, waveform_class=wf.Waveform):
        self.fisher_parameters = fisher_parameters
        self.detector = detector
        self.derivative = Derivative(waveform, parameters, detector, eps=eps, waveform_class=waveform_class)
        self.nd = len(fisher_parameters)
        self.fm = None

    def update_fm(self):
        self._fm = np.zeros((self.nd, self.nd))
        for p1 in np.arange(self.nd):
            deriv1_p = self.fisher_parameters[p1]
            deriv1 = self.derivative(deriv1_p)
            self._fm[p1, p1] = np.sum(aux.scalar_product(deriv1, deriv1, self.detector), axis=0)
            for p2 in np.arange(p1+1, self.nd):
                deriv2_p = self.fisher_parameters[p2]
                deriv2 = self.derivative(deriv2_p)
                self._fm[p1, p2] = np.sum(aux.scalar_product(deriv1, deriv2, self.detector), axis=0)
                self._fm[p2, p1] = self._fm[p1, p2]

    @property
    def fm(self):
        if self._fm is None:
            self.update_fm()
        return self._fm

    @fm.setter
    def fm(self, hardcode_fm):
        self._fm = hardcode_fm

    def __call__(self):
        return self.fm

def sky_localization_area(
    network_fisher_inverse: np.ndarray,
    declination_angle: np.ndarray,
    right_ascension_index: int,
    declination_index: int,
) -> float:
    """
    Compute the 1-sigma sky localization ellipse area starting
    from the full network Fisher matrix inverse and the inclination.
    """
    return (
        np.pi
        * np.abs(np.cos(declination_angle))
        * np.sqrt(
            network_fisher_inverse[right_ascension_index, right_ascension_index]
            * network_fisher_inverse[declination_index, declination_index]
            - network_fisher_inverse[right_ascension_index, declination_index] ** 2
        )
    )

def sky_localization_percentile_factor(percentile=90.):
    """Conversion factor to go from the sky localization area provided 
    by GWFish (one sigma, in steradians) to the X% contour, in degrees squared.
    """
    
    return - 2 * np.log(1 - percentile / 100.) * (180 / np.pi)**2

def compute_detector_fisher(
    detector: det.Detector,
    signal_parameter_values: pd.DataFrame,
    fisher_parameters: list[str],
    waveform_model: str = 'IMRPhenomD',
    waveform_class = wf.LALFD_Waveform,
    use_duty_cycle: bool = False,
):
    data_params = {
        'frequencyvector': detector.frequencyvector,
        'f_ref': 50.
    }
    waveform_obj = waveform_class(waveform_model, signal_parameter_values, data_params)
    wave = waveform_obj()
    t_of_f = waveform_obj.t_of_f

    signal = det.projection(signal_parameter_values, detector, wave, t_of_f)

    component_SNRs = det.SNR(detector, signal, use_duty_cycle)
    detector_SNR_square = np.sum(component_SNRs ** 2)

    return FisherMatrix(waveform_model, signal_parameter_values, fisher_parameters, detector, waveform_class=waveform_class).fm, detector_SNR_square

def compute_network_errors(
    network: det.Network,
    parameter_values: pd.DataFrame,
    fisher_parameters: list[str],
    waveform_model: str = 'IMRPhenomD',
    waveform_class = wf.LALFD_Waveform,
    use_duty_cycle: bool = False,
) -> tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]:
    """
    Compute Fisher matrix errors for a network whose
    SNR and Fisher matrices have already been calculated.

    Will only return output for the signals n_above_thr
    for which the network SNR is above network.detection_SNR[1].

    Returns:
    network_snr: array with shape (n_above_thr,)
        Network SNR for the detected signals.
    parameter_errors: array with shape (n_above_thr, n_parameters)
        One-sigma Fisher errors for the parameters.
    sky_localization: array with shape (n_above_thr,) or None
        One-sigma sky localization area in steradians,
        returned if the signals have both right ascension and declination,
        None otherwise.
    """

    n_params = len(fisher_parameters)
    n_signals = len(parameter_values)

    assert n_params > 0
    assert n_signals > 0

    signals_havesky = False
    if ("ra" in fisher_parameters) and ("dec" in fisher_parameters):
        signals_havesky = True
        i_ra = fisher_parameters.index("ra")
        i_dec = fisher_parameters.index("dec")

    detector_snr_thr, network_snr_thr = network.detection_SNR

    parameter_errors = np.zeros((n_signals, n_params))
    if signals_havesky:
        sky_localization = np.zeros((n_signals,))
    network_snr = np.zeros((n_signals,))

    for k in tqdm(range(n_signals)):
        network_fisher_matrix = np.zeros((n_params, n_params))

        network_snr_square = 0.
        
        signal_parameter_values = parameter_values.iloc[k]

        for detector in network.detectors:
            
            detector_fisher, detector_snr_square = compute_detector_fisher(detector, signal_parameter_values, fisher_parameters, waveform_model, waveform_class, use_duty_cycle)
            
            network_snr_square += detector_snr_square
        
            if np.sqrt(detector_snr_square) > detector_snr_thr:
                network_fisher_matrix += detector_fisher

        network_fisher_inverse, _ = invertSVD(network_fisher_matrix)
        parameter_errors[k, :] = np.sqrt(np.diagonal(network_fisher_inverse))

        network_snr[k] = np.sqrt(network_snr_square)

        if signals_havesky:
            sky_localization[k] = sky_localization_area(
                network_fisher_inverse, parameter_values["dec"].iloc[k], i_ra, i_dec
            )

    detected, = np.where(network_snr > network_snr_thr)

    if signals_havesky:
        return (
            network_snr[detected],
            parameter_errors[detected, :],
            sky_localization[detected],
        )

    return network_snr[detected], parameter_errors[detected, :], None


def errors_file_name(
    network: det.Network, sub_network_ids: list[int], population_name: str
) -> str:

    sub_network = "_".join([network.detectors[k].name for k in sub_network_ids])

    return (
        "Errors_"
        + sub_network
        + "_"
        + population_name
        + "_SNR"
        + str(network.detection_SNR[1])
    )


def output_to_txt_file(
    parameter_values: pd.DataFrame,
    network_snr: np.ndarray,
    parameter_errors: np.ndarray,
    sky_localization: Optional[np.ndarray],
    fisher_parameters: list[str],
    filename: str,
) -> None:

    delim = " "
    header = (
        "network_SNR "
        + delim.join(parameter_values.keys())
        + " "
        + delim.join(["err_" + x for x in fisher_parameters])
    )
    save_data = np.c_[network_snr, parameter_values, parameter_errors]
    if sky_localization is not None:
        header += " err_sky_location"
        save_data = np.c_[save_data, sky_localization]

    row_format = "%s " + " ".join(["%.3E" for _ in range(save_data.shape[1] - 1)])

    np.savetxt(
        filename + ".txt",
        save_data,
        delimiter=" ",
        header=header,
        comments="",
        fmt=row_format,
    )


def errors_file_name(
    network: det.Network, sub_network_ids: list[int], population_name: str
) -> str:

    sub_network = "_".join([network.detectors[k].name for k in sub_network_ids])

    return (
        "Errors_"
        + sub_network
        + "_"
        + population_name
        + "_SNR"
        + str(network.detection_SNR[1])
    )

def analyze_and_save_to_txt(
    network: det.Network,
    parameter_values: pd.DataFrame,
    fisher_parameters: list[str],
    sub_network_ids_list: list[list[int]],
    population_name: str,
    **kwargs
) -> None:

    for sub_network_ids in sub_network_ids_list:

        partial_network = network.partial(sub_network_ids)

        network_snr, errors, sky_localization = compute_network_errors(
            network=network,
            parameter_values=parameter_values,
            fisher_parameters=fisher_parameters,
            **kwargs,
        )

        filename = errors_file_name(
            network=network,
            sub_network_ids=sub_network_ids,
            population_name=population_name,
        )

        output_to_txt_file(
            parameter_values=parameter_values,
            network_snr=network_snr,
            parameter_errors=errors,
            sky_localization=sky_localization,
            fisher_parameters=fisher_parameters,
            filename=filename,
        )
        
