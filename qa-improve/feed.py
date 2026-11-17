# builds a two-channel timeline from audio/N-CH.aiff and streams frames to stdout ~real time (speed x)
import sys, glob, os, subprocess, struct, time, numpy as np
SR=16000; speed=float(sys.argv[1]) if len(sys.argv)>1 else 1.0
files=sorted(glob.glob("/Users/yaniv/Projects/timmy/qa-improve/audio/*.aiff"), key=lambda p:int(os.path.basename(p).split("-")[0]))
tl={0:[],1:[]}
for f in files:
    ch=int(os.path.basename(f).split("-")[1][0])
    a=np.frombuffer(subprocess.run(["ffmpeg","-v","error","-i",f,"-f","f32le","-ac","1","-ar","16000","-"],capture_output=True).stdout,dtype=np.float32)
    gap=np.zeros(int(1.2*SR),np.float32)
    tl[ch].append(np.concatenate([a,gap])); tl[1-ch].append(np.zeros(len(a)+len(gap),np.float32))
c={k:np.concatenate(v) for k,v in tl.items()}
sys.stderr.write(f"total audio {len(c[0])/SR:.1f}s\n")
step=int(0.1*SR); out=sys.stdout.buffer
for i in range(0,len(c[0]),step):
    for ch in (0,1):
        a=c[ch][i:i+step]; out.write(bytes([ch])+struct.pack("<I",len(a))+a.astype("<f4").tobytes())
    out.flush(); time.sleep(0.1/speed)
