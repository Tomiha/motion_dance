#!/usr/bin/python
#
import warnings
warnings.filterwarnings('ignore')
import signal, argparse, colorama, h5py, os
import sys, threading, platform, time, imp 
from utillib.print_utils import print_info, print_warning, print_error
from time import sleep
import numpy as np
from sys import stdout
from six.moves import queue
from utillib.audio import single_spectrogram
import chainer
from chainer import cuda, serializers, Variable
import soundfile
from transforms3d.euler import quat2euler
from motion_format import render_motion

try:
  import vlc
  vlclib=True
except Exception as e:
  print_warning('vlc library not found')
  vlclib=False
  pass

if sys.version_info[0] < 3:
  import OSC #install python-osc
  pyver3 = False
else:
  from pythonosc import osc_message_builder, udp_client
  pyver3 = True


list_address = ['PosBody', 'RotPelvis', 'RotHead', 'RotNeck','RotSpine1','RotSpine2','RotLeftUp','RotRightUp', #last indx: 7
  'RotLeftLow', 'RotRightLow','RotLeftThigh','RotRightThigh', 'RotLeftCalf', 'RotRightCalf', 'RotLeftFoot',  #last indx: 14
  'RotRightFoot','RotLeftClav','RotRightClav']

def datafeed():
  data_w.put('start')
  print('Converting audio data')
  loc = sec = i = 0
  rsmpfile = 'resampled.wav'
  if platform == 'Windows':
    cmmd = 'ffmpeg -y -i {} -acodec pcm_s16le -ar 16000 -ac 1 {}'.format(args.track, rsmpfile)
    subprocess.Popen(cmmd, shell=False).communicate() 
  elif platform == 'Linux':
    os.system('sox {} -c 1 -r 16000 {}'.format(args.track, rsmpfile))
  else:
    data_w.put('fail')
    print('OS not supported')
    return
  data_wav, fs  = soundfile.read(rsmpfile)
  data_wav /= np.amax(np.abs(data_wav))
  idxs = np.linspace(0, fs, 31, endpoint=True, dtype=np.int)
  rest = [0.0325, 0.0335, 0.0325]

  slope = (rng[1]-rng[0]) / (audio_max-audio_min)
  intersec = rng[1] - slope * audio_max
  data_w.put('start')
  if vlclib:
    vlcplayer.play()
  if enable_record:
    ws.call(requests.StartRecording())
  while loc < data_wav.shape[0]:
    t = time.time()
    prv = idxs[i]+fs*sec
    loc = idxs[i+1]+fs*sec
    stft_data = single_spectrogram(data_wav[prv:loc], fs, 160, 80)
    stft_data =  (stft_data * slope) + intersec
    stft_data =  np.swapaxes(stft_data, 0,1)
    if stft_data.shape[1] != 5:
      stft_data = np.ones((129, 5), dtype=np.float32)*rng[0]
      data_w.put((0, stft_data.copy()))
      break
    data_w.put((0, stft_data.copy()))
    sleep(rest[i%3])
    if i >= 29:
      i=0
      sec +=1
    else:
      i +=1
  os.remove(rsmpfile)
  data_w.put('end')
  return

def netdancer():
  if pyver3:
    c = udp_client.SimpleUDPClient(args.host, args.port_osc)
  else:
    c = OSC.OSCClient()
    c.connect((args.host, args.port_osc))  

  with h5py.File(args.minmax, 'r') as f:
    pos_min = f['minmax'][0,:][None,:] 
    pos_max = f['minmax'][1,:][None,:] 
  div = (pos_max-pos_min)
  div[div==0] = 1
  slope_pos = (rng_pos[1] - rng_pos[0] )/ div
  intersec_pos = rng_pos[1] - slope_pos * pos_max

  net = imp.load_source('Network', args.model) 
  audionet = imp.load_source('Network', './models/audio_nets.py')
  model = net.Dancer(args.initOpt, getattr(audionet, args.encoder))
  ext = os.path.basename(args.pretrained).split('.')
  ext = ext[1] if len(ext)>1 else None
  try:
    if ext == 'model':
      serializers.load_hdf5(args.pretrained, model)
    else:
      print(args.pretrained)
      serializers.load_npz(args.pretrained, model)
  except Exception as e:
    raise e

  model.to_gpu()
  #current_step = np.random.randn(1,args.initOpt[2]).astype(np.float32)
  current_step = np.zeros((1, args.initOpt[2]), dtype=np.float32)
  state = model.state
  start = False
  config ={'rot' : 'quat'}
  frame = 0
  max_frames = 8000
  feats=np.zeros((max_frames,args.initOpt[1]))
  while True:
    while data_w.empty():
      sleep(0.01)
    inp = data_w.get()
    if isinstance(inp, str):
      if inp== 'fail':
        print('Error during music processing... Exit')
        break
      elif inp=='start':
        start = True
        if not pyver3:
          oscmsg = OSC.OSCMessage()
          oscmsg.setAddress('WidgetTV')
          oscmsg.append(youtube_link.format(videolink))
          c.send(oscmsg)
        continue
      elif inp=='end':
        if enable_record:
          ws.call(requests.StopRecording())
          ws.disconnect()
        start=False
        fn=os.path.basename(args.track).split('.')[0]
        fn=args.pretrained.replace('trained/endtoend/trained.model', 'untrained/{}_feats.h5'.format(fn))
        with h5py.File(fn, 'w') as f:
          ds=f.create_dataset('feats',data=feats[0:frame])
        break

    if start:
      t = time.time()
      _, audiodata = inp
      try:
        with chainer.no_backprop_mode(), chainer.using_config('train', False):
          _h, state, current_step= model.forward(state, Variable(xp.asarray(current_step)), 
            model.audiofeat(Variable(xp.asarray(audiodata[None, None, :,:]))), True)
      except Exception as e:
        print(audiodata.shape)
        raise e
      
      current_step = chainer.cuda.to_cpu(current_step.data)
      if frame < max_frames:
        feats[frame] = chainer.cuda.to_cpu(_h.data)

      predicted_motion = (current_step -intersec_pos)/ slope_pos
      rdmt = render_motion(predicted_motion, config, scale=args.height)
      for i in range(len(list_address)):
        if pyver3:
          oscmsg = osc_message_builder.OscMessageBuilder(address=args.character) 
          oscmsg.add_arg(list_address[i])
          if i == 0: 
            oscmsg.add_arg(float(rdmt[0,0]/10.0))
            oscmsg.add_arg(float(rdmt[0,1]/-10.0))
            oscmsg.add_arg(float(rdmt[0,2]/10.0+args.height))
          else:
            oscmsg.add_arg(float(rdmt[0,i*3]))
            oscmsg.add_arg(float(rdmt[0,i*3+1]*-1.))
            oscmsg.add_arg(float(rdmt[0,i*3+2]*-1.))
          c.send(oscmsg.build())
        else:
          oscmsg = OSC.OSCMessage()
          oscmsg.setAddress(args.character)
          oscmsg.append(list_address[i]) 
          if i == 0:  
            msg =  [rdmt[0,0]/10.0, rdmt[0,1]/-10.0, rdmt[0,2]/10.0+args.height]
          else:
            msg =  [rdmt[0,i*3], rdmt[0,i*3+1]*-1., rdmt[0,i*3+2]*-1.]
          oscmsg += msg
          c.send(oscmsg)
      frame +=1
      stdout.write('Frame: {:06d}, time: {:.03f}\r'.format(frame, time.time()-t))
      stdout.flush()
  return

def signal_handler(signal, frame):
  rsmpfile = 'resampled.wav'
  if enable_record:
    try:
      ws.call(requests.StopRecording())
      ws.disconnect()
    except Exception as e:
      pass
  try:
    os.remove(rsmpfile)
  except OSError:
    pass
  if vlclib:
    vlcplayer.stop()
  print('\nBye')
  sys.exit(0)


def main():
  feeder = threading.Thread(target=datafeed)
  feeder.daemon= True
  feeder.start()
  netdancer()
  feeder.join()
  print('\nBye')

if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='UE4-Chainer DNN OSC connection')
  parser.add_argument('--track', '-w', type=str, help='Audio file')
  parser.add_argument('--model', '-m', type=str, help='Model definition in python')
  parser.add_argument('--pretrained', '-t', type=str, help='Pretrained network')
  parser.add_argument('--encoder', '-e', type=str, help='Audio encoder')
  parser.add_argument('--minmax', '-x', type=str, help='Minmax File')
  parser.add_argument('--port_osc', type=int, help='OSC ip port', default=6060)
  parser.add_argument('--port_osb', type=int, help='OSB ip port', default=4444)
  parser.add_argument('--gpu', '-g', type=int, help='GPU id', default=0)
  parser.add_argument('--initOpt', '-i',  nargs='+', type=int, help='Model initial options')
  parser.add_argument('--height', '-l', type=float, help='Height of the dancer', default=100.0)
  parser.add_argument('--character', '-c', type=str, help='UE Character name definition')
  parser.add_argument('--record', '-r', type=int, help='Record Screen', default=0)
  parser.add_argument('--host', type=str, help='UE Server ip')

  args = parser.parse_args()

  signal.signal(signal.SIGINT, signal_handler) 
  data_w = queue.Queue(maxsize=1)
  chainer.cuda.get_device_from_id(args.gpu).use()
  platform = platform.system()
  if platform == 'Windows':
    import subprocess
    win_cmd = 'wmic path win32_VideoController get Name | findstr /C:"NVIDIA"'
    names_gpu  = subprocess.check_output(win_cmd, shell=True).decode("utf-8")
    gpu_name = names_gpu.split('\r')[0]
  elif platform =="Linux":
    import os
    names_gpu = os.popen('lspci | grep NVIDIA | grep controller').read().split('\n')
    try:
      _, gpu_name = names_gpu[args.gpu].split('[')
      gpu_name, _ = gpu_name.split(']')
    except:
      gpu_name = ""
  else:
    raise OSError('OS not supported')
  
  enable_record=False

  if args.record > 0:
    from obswebsocket import obsws, requests
    enable_record=True
    ws = obsws(args.host, args.port_osb, None)
    ws.connect()
  print('Using gpu id:{} - {}'.format(args.gpu, gpu_name))
  
  youtube_link = 'https://www.youtube.com/tv#/watch?v={}'
  rng_pos = [-0.9, 0.9]
  rng = [-0.9, 0.9]
  audio_max = 5.
  audio_min = -120.
  xp = cuda.cupy
  DATA_FOLDER=os.environ['DATA_EXTRACT']
  with open('{}/Annotations/youtube_links.txt'.format(DATA_FOLDER)) as f:
    links = f.readlines()
  flname = os.path.basename(args.track).split('.')[0]
  videolink = [ x.split('\t')[1] for x in links if flname in x ][0]
  if vlclib:
    vlcplayer = vlc.MediaPlayer(args.track)
  main()