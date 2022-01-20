#
#  Pocket SDR Python Library - Fundamental GNSS SDR Functions
#
#  Author:
#  T.TAKASU
#
#  History:
#  2021-12-01  1.0  new
#  2021-12-24  1.1  fix several problems
#  2022-01-13  1.2  add API unpack_data()
#                   DOP_STEP: 0.25 -> 0.5
#  2022-01-20  1.3  use external library for mix_carr(), corr_std(), corr_fft()
#                   modify API search_sig(), search_code(), mix_carr() 
#
from math import *
from ctypes import *
import time, os, platform
import numpy as np
from numpy import ctypeslib
import scipy.fftpack as fft
import sdr_code

# load external library --------------------------------------------------------
env = platform.platform()
dir = os.path.dirname(__file__)
try:
    if 'Windows' in env:
        libsdr = cdll.LoadLibrary(dir + '/../lib/win32/libsdr.so')
    elif 'Linux' in env:
        libsdr = cdll.LoadLibrary(dir + '/../lib/linux/libsdr.so')
    else:
        libsdr = None
except:
    libsdr = None

# constants --------------------------------------------------------------------
MAX_DOP  = 5000.0  # default max Doppler frequency to search signals (Hz)
DOP_STEP = 0.5     # Doppler frequency search step (* 1 / code cycle)
#DOP_STEP = 0.25    # Doppler frequency search step (* 1 / code cycle)
LIBSDR_ENA = True  # enable flag of LIBSDR

# global variable --------------------------------------------------------------
carr_tbl = []      # carrier lookup table 
log_lvl = 3        # log level
log_fp = None      # log file pointer

#-------------------------------------------------------------------------------
#  Read digitalized IF (inter-frequency) data from file. Supported file format
#  is signed byte (int8) for I-sampling (real-sampling) or interleaved singned
#  byte for IQ-sampling (complex-sampling).
#
#  args:
#      file     (I) Digitalized IF data file path
#      fs       (I) Sampling frequency (Hz)
#      IQ       (I) Sampling type (1: I-sampling, 2: IQ-sampling)
#      T        (I) Sample period (s)
#      toff=0.0 (I) Time offset from the beginning (s) (optional)
#
#  returns:
#      data     Digitized IF data as complex64 ndarray (length == 0: read error)
#
def read_data(file, fs, IQ, T, toff=0.0):
    off = int(fs * toff * IQ)
    cnt = int(fs * T * IQ) if T > 0.0 else -1 # all if T=0.0
    
    raw = np.fromfile(file, dtype=np.int8, offset=off, count=cnt)
    
    if len(raw) < cnt:
        return np.array([], dtype='complex64')
    elif IQ == 1: # I-sampling
        return np.array(raw, dtype='complex64')
    else: # IQ-sampling
        return np.array(raw[0::2] - raw[1::2] * 1j, dtype='complex64')

#-------------------------------------------------------------------------------
#  Search signals in digitized IF data. The signals are searched by parallel
#  code search algorithm in the Doppler frequencies - code offset space with or
#  w/o zero-padding option.
#
#  args:
#      sig      (I) Signal type as string ('L1CA', 'L1CB', 'L1CP', ....)
#      prn      (I) PRN number
#      data     (I) Digitized IF data as complex64 ndarray
#      fs       (I) Sampling frequency (Hz)
#      fi       (I) IF frequency (Hz)
#      max_dop  (I) Max Doppler frequency for signal search (Hz) (optional)
#      zero_pad (I) Zero-padding option for singal search (optional)
#
#  returns:
#      P        Normalized correlation powers in the Doppler frequencies - Code
#               offset space as float32 2D-ndarray
#      fds      Doppler frequencies for signal search as ndarray (Hz)
#      coffs    Code offsets for signal search as ndarray (s)
#      ix       Index of position with max correlation power in the search space
#               (ix[0]: in Doppler frequencies, ix[1]: in Code offsets)
#      cn0      C/N0 of max correlation power (dB-Hz)
#      dop      Fine Doppler frequnecy estimated (Hz)
#
def search_sig(sig, prn, data, fs, fi, max_dop=MAX_DOP, zero_pad=True):
    # generate code
    code = sdr_code.gen_code(sig, prn)
    
    if len(code) == 0:
        return [], [0.0], [0.0], (0, 0), 0.0, 0.0
    
    # shift IF frequency for GLONASS FDMA
    fi = shift_freq(sig, prn, fi)
    
    # generate code FFT
    T = sdr_code.code_cyc(sig)
    N = int(fs * T)
    code_fft = sdr_code.gen_code_fft(code, T, 0.0, fs, N, N if zero_pad else 0)
    
    # doppler search bins
    fds = dop_bins(T, max_dop)
    
    # parallel code search and non-coherent integration
    P = np.zeros((len(fds), N), dtype='float32')
    for i in range(0, len(data) - len(code_fft) + 1, N):
        P += search_code(code_fft, T, data, i, fs, fi, fds)
    
    # max correlation power and C/N0
    P_max, ix, cn0 = corr_max(P, T)
    
    coffs = np.arange(0, T, 1.0 / fs, dtype='float32')
    
    return P / P_max, fds, coffs, ix, cn0, fine_dop(P.T[ix[1]], fds, ix[0])

#-------------------------------------------------------------------------------
#  Parallel code search in digitized IF data.
#
#  args:
#      code_fft (I) Code DFT (with or w/o zero-padding)
#      T        (I) Code cycle (period) (s)
#      buff     (I) Buffer of IF data as complex64 ndarray
#      ix       (I) index of buffer
#      fs       (I) Sampling frequency (Hz)
#      fi       (I) IF frequency (Hz)
#      fds      (I) Doppler frequency bins as ndarray (Hz)
#
#  returns:
#      P        Correlation powers in the Doppler frequencies - Code offset
#               space as float32 2D-ndarray
#
def search_code(code_fft, T, buff, ix, fs, fi, fds):
    N = int(fs * T)
    P = np.zeros((len(fds), N), dtype='float32')
    
    for i in range(len(fds)):
        data_carr = mix_carr(buff, ix, len(code_fft), fs, fi + fds[i], 0.0)
        P[i] = np.abs(corr_fft(data_carr, code_fft)[0:N]) ** 2
    return P

# max correlation power and C/N0 -----------------------------------------------
def corr_max(P, T):
    ix = np.unravel_index(np.argmax(P), P.shape)
    P_max = P[ix[0]][ix[1]]
    P_ave = np.mean(P)
    cn0 = 10.0 * log10((P_max - P_ave) / P_ave / T) if P_ave > 0.0 else 0.0
    return P_max, ix, cn0

# fine Doppler frequency by quadratic fitting ----------------------------------
def fine_dop(P, fds, ix):
    if ix == 0 or ix == len(fds) - 1:
        return fds[ix]
    p = np.polyfit(fds[ix-1:ix+2], P[ix-1:ix+2], 2)
    return -p[1] / (2.0 * p[0])

# shift IF frequency for GLONASS FDMA ------------------------------------------
def shift_freq(sig, fcn, fi):
    if sig == 'G1CA':
        fi += 0.5625e6 * fcn
    elif sig == 'G2CA':
        fi += 0.4375e6 * fcn
    return fi

# doppler search bins ----------------------------------------------------------
def dop_bins(T, max_dop):
    return np.arange(-max_dop, max_dop + DOP_STEP / T, DOP_STEP / T)

# mix carrier ------------------------------------------------------------------
def mix_carr(buff, ix, N, fs, fc, phi):
    if libsdr and LIBSDR_ENA:
        data_carr = np.empty(N, dtype='complex64')
        libsdr.mix_carr.argtypes = [
            ctypeslib.ndpointer('complex64', flags='C'), c_int32, c_int32,
            c_double, c_double, c_double,
            ctypeslib.ndpointer('complex64', flags='C')]
        libsdr.mix_carr(buff, ix, N, fs, fc, phi, data_carr)
        return data_carr
    else:
        n = 256 # carrier lookup table size
        global carr_tbl
        if len(carr_tbl) == 0:
            carr_tbl = np.array(np.exp(-2j * np.pi * np.arange(n) / n),
                           dtype='complex64')
        i = ((fc / fs * np.arange(N) + phi) * n).astype('int')
        return buff[ix:ix+N] * carr_tbl[i % n]

# standard correlator ----------------------------------------------------------
def corr_std(data, code, pos):
    if libsdr and LIBSDR_ENA:
        corr = np.empty(len(pos), dtype='complex64')
        pos = np.array(pos, dtype='int32')
        libsdr.corr_std.argtypes = [
            ctypeslib.ndpointer('complex64', flags='C'),
            ctypeslib.ndpointer('complex64', flags='C'), c_int32,
            ctypeslib.ndpointer('int32', flags='C'), c_int32,
            ctypeslib.ndpointer('complex64', flags='C')]
        libsdr.corr_std(data, code, len(data), pos, len(pos), corr)
        return corr
    else:
        N = len(data)
        corr = np.zeros(len(pos), dtype='complex64')
        for i in range(len(pos)):
            if pos[i] > 0:
                corr[i] = np.dot(data[pos[i]:], code[:-pos[i]]) / (N - pos[i])
            elif pos[i] < 0:
                corr[i] = np.dot(data[:pos[i]], code[-pos[i]:]) / (N + pos[i])
            else:
                corr[i] = np.dot(data, code) / N
        return corr

# FFT correlator ---------------------------------------------------------------
def corr_fft(data, code_fft):
    if libsdr and LIBSDR_ENA:
        corr = np.empty(len(data), dtype='complex64')
        libsdr.corr_fft.argtypes = [
            ctypeslib.ndpointer('complex64', flags='C'),
            ctypeslib.ndpointer('complex64', flags='C'), c_int32,
            ctypeslib.ndpointer('complex64', flags='C')]
        libsdr.corr_fft(data, code_fft, len(data), corr)
        return corr
    else:
        return fft.ifft(fft.fft(data) * code_fft) / len(data)

# open log ---------------------------------------------------------------------
def log_open(file):
    global log_fp
    try:
        log_fp = open(file, 'w')
    except:
        print('log open error %s' % (file))

# close log --------------------------------------------------------------------
def log_close():
    global log_fp
    log_fp.close()
    log_fp = None

# set log level ----------------------------------------------------------------
def log_level(level):
    global log_lvl
    log_lvl = level

# output log -------------------------------------------------------------------
def log(level, msg):
    global log_fp, log_lvl
    if log_lvl == 0:
        print(msg)
    elif log_fp and level <= log_lvl:
        log_fp.write(msg + '\r\n')
        log_fp.flush()

# parse numbers list and range -------------------------------------------------
def parse_nums(str):
    nums = []
    for ss in str.split(','):
        s = ss.split('-')
        if len(s) >= 4:
            if s[0] == '' and s[2] == '': # -n--m
                nums += range(-int(s[1]), -int(s[3]) + 1)
        elif len(s) >= 3:
            if s[0] == '': # -n-m
                nums += range(-int(s[1]), int(s[2]) + 1)
        elif len(s) >= 2:
            if s[0] == '': # -n
                nums += [-int(s[1])]
            else: # n-m
                nums += range(int(s[0]), int(s[1]) + 1)
        else: # n
            nums += [int(s[0])]
    return nums

# add item to buffer -----------------------------------------------------------
def add_buff(buff, item):
    buff[:-1], buff[-1] = buff[1:], item

# pack bits to uint8 ndarray ---------------------------------------------------
def pack_bits(data, nz=0):
    if nz > 0:
        data = np.hstack([[0] * nz, data])
    N = len(data)
    buff = np.zeros((N + 7) // 8, dtype='uint8')
    for i in range(N):
        buff[i // 8] |= (data[i] << (7 - i % 8))
    return buff

# unpack uint8 ndarray to bits ------------------------------------------------
def unpack_bits(data, N):
    buff = np.zeros(N, dtype='uint8')
    for i in range(np.min([N, len(data) * 8])):
        buff[i] = (data[i // 8] >> (7 - i % 8)) & 1
    return buff

# unpack data to bits ----------------------------------------------------------
def unpack_data(data, N):
    buff = np.zeros(N, dtype='uint8')
    for i in range(N):
        buff[i] = (data >> (N - 1 - i)) & 1
    return buff

# exclusive-or of all bits ------------------------------------------------------
def xor_bits(X):
    return bin(X).count('1') % 2

# hex string --------------------------------------------------------------------
def hex_str(data):
    str = ''
    for i in range(len(data)):
        str += '%02X' % (data[i])
    return str
