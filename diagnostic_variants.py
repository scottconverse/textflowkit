from pathlib import Path
import os,sys,json,wave,time,dataclasses
R=Path(__file__).resolve().parent;L=R
mode=sys.argv[1]
os.environ.update(HF_HUB_DISABLE_TELEMETRY='1',FERMION_DEVICE='cpu',FERMION_CPU_THREADS='16')
if mode=='fp32':os.environ.update(FERMION_P2_CPU='fp32',FERMION_P2_CPU_TDT='torch',FERMION_P2_CPU_ENC='torch')
if mode in ('torch-decoder','candidate'):os.environ['FERMION_P2_CPU_TDT']='torch'
if mode=='torch-encoder':os.environ['FERMION_P2_CPU_ENC']='torch'
if mode=='single-thread':os.environ['FERMION_CPU_THREADS']='1'
if mode=='avx2':os.environ['FERMION_CPU_TIER']='avx2'
import numpy as np,fermion
start=time.perf_counter();m=fermion.load_speech('phonon-2');out={'mode':mode,'load_seconds':time.perf_counter()-start,'description':m.describe(),'clips':[]}
if mode in ('true-frames','candidate'):
 import torch
 from fermion._speech.engine_phonon2_cpu import PREEMPH,N_FFT,HOP,WIN,LOG_GUARD
 def corrected_mel(wave):
  x=torch.as_tensor(np.asarray(wave,dtype=np.float32))[None]
  n=x.shape[1]//HOP
  x=torch.cat([x[:,:1],x[:,1:]-PREEMPH*x[:,:-1]],dim=1)
  st=torch.stft(x,N_FFT,hop_length=HOP,win_length=WIN,window=m._window,return_complex=True,pad_mode='constant')
  mag=torch.sqrt(torch.view_as_real(st).pow(2).sum(-1)).pow(2)
  mel=torch.log(m._melf@mag+LOG_GUARD).permute(0,2,1)[:,:n,:]
  mean=mel.mean(1,keepdim=True);var=((mel-mean)**2).sum(1)/(n-1)
  return (mel-mean)/(torch.sqrt(var).unsqueeze(1)+1e-5),torch.ones((1,n),dtype=torch.long)
 m._log_mel=corrected_mel
for i in [2,361,441]:
 with wave.open(str(L/f'diagnostic-chunk-{i:04d}.wav')) as w:x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32768
 t=time.perf_counter();res=m.transcribe_array_detailed(x);v={'index':i,'seconds':time.perf_counter()-t,'result':dataclasses.asdict(res)};out['clips'].append(v)
 print(json.dumps({'mode':mode,'index':i,'seconds':v['seconds'],'text':res.text},ensure_ascii=False),flush=True)
(R/f'{mode}.json').write_text(json.dumps(out,indent=2,ensure_ascii=False),encoding='utf-8')

