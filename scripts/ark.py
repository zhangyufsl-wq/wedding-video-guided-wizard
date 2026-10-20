#!/usr/bin/env python3
"""国内模型适配：火山方舟 Seedream（角色定妆、分镜首帧）与 Seedance（首帧生视频），以及交付后清理真人原图。

每个付费调用都绑定当前步骤与已确认版本，需要 --authorized；失败不自动重试，超时保留任务号供 fetch 续取。
"""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

_here=Path(__file__).resolve().parent
def _load(name,file):
    spec=importlib.util.spec_from_file_location(name,_here/file)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
m=_load('wedding_media','media.py');w=m.w

ARK_BASES=('https://ark.cn-beijing.volces.com/api/v3',)
DEFAULT_IMAGE_MODEL='doubao-seedream-5-0-flash-260915'
DEFAULT_VIDEO_MODEL='doubao-seedance-2-0-mini-260615'
PLACEHOLDER=re.compile(r'\{\{[^}]+\}\}|\[待填\]|TODO')
SAFE_ID=re.compile(r'[A-Za-z0-9_-]{1,48}')
# Seedream 要求总像素在约 3.69M–16.78M 之间。
SIZES={'16:9':'2560x1440','9:16':'1440x2560','1:1':'2048x2048','4:3':'2304x1728','3:4':'1728x2304'}
MIME={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}

def config():
    key=os.environ.get('ARK_API_KEY')
    if not key:raise ValueError('未配置 ARK_API_KEY；在本机环境变量里设置，不要贴到对话或仓库')
    base=os.environ.get('ARK_BASE_URL',ARK_BASES[0]).rstrip('/')
    if base not in ARK_BASES:raise ValueError('内置适配仅支持火山方舟官方地址')
    return key,base

def text_file(project,path,what):
    t=w.inside(project,path).read_text(encoding='utf-8').strip()
    if not t or PLACEHOLDER.search(t):raise ValueError(what+'为空或仍有占位符')
    return t

def data_uri(path,max_side=2048):
    p=Path(path);mime=MIME.get(p.suffix.lower())
    if not mime:raise ValueError('不支持的图片类型：'+p.name)
    data=p.read_bytes()
    try:
        from PIL import Image, ImageOps
        import io
        with Image.open(p) as im:
            # 手机原图常超过 10MB 或带旋转信息：统一转正、缩到长边 2048、存成 JPEG，只在内存里处理，不改原文件。
            if len(data)>4*1024*1024 or max(im.size)>max_side or (im.getexif() or {}).get(274,1)!=1:
                im=ImageOps.exif_transpose(im).convert('RGB');im.thumbnail((max_side,max_side))
                buf=io.BytesIO();im.save(buf,'JPEG',quality=90);data=buf.getvalue();mime='image/jpeg'
    except ImportError:pass
    if len(data)>10*1024*1024:raise ValueError('参考图超过 10MB，先压缩：'+p.name)
    return f'data:{mime};base64,'+base64.b64encode(data).decode()

def check_image(path):
    try:from PIL import Image
    except ImportError:raise ValueError('校验图片需要 Pillow：python3 -m pip install Pillow') from None
    try:
        with Image.open(path) as im:im.verify()
        with Image.open(path) as im:im.load()
    except Exception:raise ValueError('不是可解码图片：'+Path(path).name) from None

def call(method,url,key,payload=None,timeout=180):
    data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
    req=urllib.request.Request(url,data=data,method=method,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(m.NoRedirect).open(req,timeout=timeout) as r:return json.load(r)
    except urllib.error.HTTPError as e:
        try:detail=json.load(e).get('error',{})
        except Exception:detail={}
        msg=' '.join(str(detail.get(k)) for k in ('code','param','message') if detail.get(k))
        raise ValueError(f'方舟接口返回 HTTP {e.code} {msg}'.strip()+'；先查原因，勿自动重复付费请求') from None
    except ValueError:raise
    except Exception:
        raise ValueError('方舟请求未取得可验证结果；先到控制台查任务状态，勿自动重复付费请求') from None

def download(url,dest):
    if not str(url).startswith('https://'):raise ValueError('生成结果地址不是 https')
    # 结果是预签名地址：不携带方舟密钥。
    with urllib.request.urlopen(urllib.request.Request(url),timeout=300) as r,Path(dest).open('xb') as f:
        size=0
        for chunk in iter(lambda:r.read(1024*1024),b''):f.write(chunk);size+=len(chunk)
    if not size:Path(dest).unlink();raise ValueError('下载结果为空')
    return dest

def rel(project,p):return Path(p).resolve().relative_to(Path(project).resolve()).as_posix()

def sidecar(project,dest,record):
    m.write_new(Path(dest).with_suffix('.gen.json'),json.dumps({**record,'output':rel(project,dest),'output_sha256':w.sha(dest),'created_at':w.now(),
        'note':'接口成功不代表像本人或满意；仍需制作方看图确认'},ensure_ascii=False,indent=2))

def seedream(project,prompt,images,size,dest,seed=None):
    key,base=config()
    # 只发各版本通用的参数：部分轻量模型（如 5.0-flash 只出单图）不接受组图参数。
    payload={'model':os.environ.get('ARK_IMAGE_MODEL',DEFAULT_IMAGE_MODEL),'prompt':prompt,'size':size,
             'response_format':'url','watermark':os.environ.get('ARK_WATERMARK','false').lower()=='true'}
    if images:payload['image']=[data_uri(x) for x in images] if len(images)>1 else data_uri(images[0])
    if seed is not None:payload['seed']=seed
    data=call('POST',base+'/images/generations',key,payload)
    items=data.get('data') or []
    if not items or not items[0].get('url'):raise ValueError('未返回图片结果')
    download(items[0]['url'],dest);check_image(dest)
    return payload['model']

# ---------- 第7步：角色定妆 ----------
def character(project,name,photos,style,out,authorized,seed=None,outfit=None):
    m.stage(project,{7})
    if not authorized:raise ValueError('需当前任务明确授权生图 API 调用')
    if not name.strip() or not photos:raise ValueError('需要角色名和至少一张本人照片')
    dest=m.new_outputs(project,out,['.gen.json'])
    style_text=text_file(project,style,'画风锚点')
    refs=[w.inside(project,x) for x in photos]
    for r in refs:check_image(r)
    wear=f'服装统一画成：{outfit}（不要照搬照片里的礼服或婚纱）。' if outfit else '服饰按照片还原。'
    prompt=(f'{style_text}\n\n任务：根据参考照片，为「{name}」生成一张Q版角色设定图。同一画面包含正面、四分之三侧面、全身三个视角，'
            '浅色纯色背景，不加任何文字或标注。严格按照片还原发型发色、脸型、五官特征；照片里没有的东西（如眼镜、帽子、饰品）一律不要添加；不要美化到认不出，'
            f'不改变年龄阶段和性别表现。{wear}所有参考图都是同一个人：{name}。')
    model=seedream(project,prompt,refs,'2048x2048',dest,seed)
    sidecar(project,dest,{'kind':'character-sheet','name':name,'model':model,'seed':seed,'outfit':outfit,'prompt':prompt,
        'style':rel(project,w.inside(project,style)),'sources':[{'path':rel(project,r),'sha256':w.sha(r)} for r in refs]})
    return dest

def load_characters(project,state,path):
    p=w.inside(project,path);data=json.loads(p.read_text(encoding='utf-8'))
    chars={str(c['id']):c for c in data.get('characters',[])}
    if not chars or not data.get('style'):raise ValueError('CHARACTERS.json 需要 style 与 characters')
    if state['current_step']==8:
        approved=m.hashes(state,7)
        if w.sha(p) not in approved:raise ValueError('角色表不是第7步确认版本')
        for c in chars.values():
            if w.sha(w.inside(project,c['sheet'])) not in approved:raise ValueError('角色设定图不是第7步确认版本：'+c['id'])
    return data,chars

# ---------- 第7/8步：分镜首帧 ----------
def keyframe(project,shot,prompt_path,characters,refs,out,ratio,authorized,seed=None):
    state=m.stage(project,{7,8})
    if not authorized:raise ValueError('需当前任务明确授权生图 API 调用')
    if not SAFE_ID.fullmatch(shot):raise ValueError('不安全的镜号')
    if ratio not in SIZES:raise ValueError('画幅仅支持 '+'、'.join(SIZES))
    data,chars=load_characters(project,state,characters)
    dest=m.new_outputs(project,out,['.gen.json'])
    unknown=[r for r in refs if r not in chars]
    if unknown:raise ValueError('角色表里没有：'+','.join(unknown))
    sheets=[w.inside(project,chars[r]['sheet']) for r in refs]
    for s in sheets:check_image(s)
    mapping='\n'.join(f'第{i}张参考图是「{chars[r]["name"]}」的Q版角色设定图，只用来保持此人的脸型、五官、发色和Q版比例；服装和发型细节以本镜描述为准；画面里不要出现三视图排版。'
                      for i,r in enumerate(refs,1))
    prompt='\n\n'.join(x for x in [text_file(project,data['style'],'画风锚点'),mapping,
        text_file(project,prompt_path,'本镜生图提示词'),f'画幅 {ratio}，单一画面，不加文字。'] if x)
    model=seedream(project,prompt,sheets,SIZES[ratio],dest,seed)
    sidecar(project,dest,{'kind':'keyframe','shot':shot,'model':model,'seed':seed,'ratio':ratio,'prompt':prompt,
        'characters':rel(project,w.inside(project,characters)),'references':[{'id':r,'sha256':w.sha(s)} for r,s in zip(refs,sheets)]})
    return dest

# ---------- 第9步：首帧生视频 ----------
def video_shots(project,state,manifest):
    data=json.loads(w.inside(project,manifest).read_text(encoding='utf-8'))
    plan_path=w.inside(project,data['plan'])
    if w.sha(plan_path) not in m.hashes(state,6):raise ValueError('镜头计划不是第6步确认版本')
    expected=[str(x['id']) for x in json.loads(plan_path.read_text(encoding='utf-8'))['shots']]
    shots={str(s['id']):s for s in data['shots']}
    if not expected or len(set(expected))!=len(expected) or set(shots)!=set(expected) or len(shots)!=len(data['shots']):
        raise ValueError('镜号重复、缺失或与确认计划不一致')
    approved=m.hashes(state,7)|m.hashes(state,8);out=[]
    for ident in expected:
        if not SAFE_ID.fullmatch(ident):raise ValueError('不安全的镜号')
        pic=w.inside(project,shots[ident]['image'])
        if w.sha(pic) not in approved:raise ValueError('首帧不是已确认的第7/8步图片：'+ident)
        check_image(pic)
        out.append((ident,pic,text_file(project,shots[ident]['prompt'],'视频提示词 '+ident),shots[ident]))
    return out

def poll(project,task,dest,key,base,interval,timeout,record):
    deadline=time.monotonic()+timeout
    while True:
        info=call('GET',f'{base}/contents/generations/tasks/{task}',key)
        status=info.get('status')
        if status=='succeeded':
            url=(info.get('content') or {}).get('video_url')
            if not url:raise ValueError('任务成功但未返回视频地址：'+task)
            download(url,dest)
            try:length=m.duration(dest)
            except ValueError:length=None
            sidecar(project,dest,{**record,'task_id':task,'decoded_seconds':length,'usage':info.get('usage')})
            return dest
        if status in ('failed','cancelled','expired'):
            err=(info.get('error') or {}).get('code') or status
            raise ValueError(f'视频任务 {task} 未成功：{err}；先看原因再决定是否重做')
        if time.monotonic()>deadline:
            m.write_new(Path(dest).with_suffix('.pending.json'),json.dumps({**record,'task_id':task,'status':status,'at':w.now()},ensure_ascii=False,indent=2))
            raise ValueError(f'视频任务 {task} 仍在 {status}；已记录 .pending.json，稍后用 fetch 续取，不要重新提交')
        time.sleep(interval)

def i2v(project,manifest,shot,out,duration,ratio,resolution,authorized,interval=10,timeout=900,state=None):
    state=state or m.stage(project,{9})
    if not authorized:raise ValueError('需当前任务明确授权视频 API 调用')
    if not 2<=duration<=12:raise ValueError('单镜时长需在 2–12 秒')
    items={x[0]:x for x in video_shots(project,state,manifest)}
    if shot not in items:raise ValueError('确认分镜中没有镜号：'+shot)
    ident,pic,prompt,_=items[shot]
    dest=m.new_outputs(project,out,['.gen.json','.pending.json'])
    key,base=config();model=os.environ.get('ARK_VIDEO_MODEL',DEFAULT_VIDEO_MODEL)
    flags=f' --duration {duration} --ratio {ratio} --resolution {resolution} --watermark false'
    payload={'model':model,'content':[{'type':'text','text':prompt+flags},
             {'type':'image_url','image_url':{'url':data_uri(pic)},'role':'first_frame'}]}
    task=call('POST',base+'/contents/generations/tasks',key,payload).get('id')
    if not task:raise ValueError('未返回任务号')
    record={'kind':'image-to-video','shot':ident,'model':model,'first_frame':rel(project,pic),'first_frame_sha256':w.sha(pic),
            'prompt_sha256':w.sha(w.inside(project,items[shot][3]['prompt'])),'duration':duration,'ratio':ratio,'resolution':resolution}
    return poll(project,task,dest,key,base,interval,timeout,record)

def fetch(project,task,out,interval=10,timeout=900):
    m.stage(project,{9})
    if not re.fullmatch(r'[A-Za-z0-9_-]{4,80}',task):raise ValueError('任务号格式不对')
    pending=w.inside(project,Path(out).with_suffix('.pending.json'))
    record=json.loads(pending.read_text(encoding='utf-8'))
    if record.get('task_id')!=task:raise ValueError('任务号与 .pending.json 不一致')
    dest=m.new_outputs(project,out,['.gen.json'])
    key,base=config()
    return poll(project,task,dest,key,base,interval,timeout,{k:v for k,v in record.items() if k not in ('task_id','status','at')})

def i2v_batch(project,manifest,outdir,version,max_shots,duration,ratio,resolution,authorized,interval=10,timeout=900):
    state=m.stage(project,{9})
    if not authorized:raise ValueError('需当前任务明确授权视频 API 调用')
    if not re.fullmatch(r'v[0-9]{1,3}',version):raise ValueError('版本名用 v1、v2 这种格式')
    if max_shots<1:raise ValueError('--max-shots 必须为正整数：明确本次最多付费生成几镜')
    shots=video_shots(project,state,manifest);done=[];skipped=[]
    for ident,_,_,spec in shots:
        out=Path(outdir)/f'{ident}-{version}.mp4'
        if w.inside(project,out,False).exists() or w.inside(project,out.with_suffix('.pending.json'),False).exists():
            skipped.append(ident);continue
        if len(done)>=max_shots:break
        # 失败即停：不连续付费重试，也不跳过继续烧钱。
        i2v(project,manifest,ident,out,int(spec.get('duration',duration)),ratio,resolution,True,interval,timeout,state)
        done.append(ident)
    remaining=[x[0] for x in shots if x[0] not in done and x[0] not in skipped]
    return {'generated':done,'skipped_existing':skipped,'remaining':remaining}

# ---------- 交付后：删除真人原图 ----------
def purge(project,directory,confirm):
    s=w.validate(project)
    if not s['completed']:raise ValueError('新人验收完成后才能清理原图')
    if not confirm:raise ValueError('删除不可恢复：确认要删时加 --confirm')
    root=Path(project).resolve();folder=w.inside(project,directory,False)
    if not folder.is_dir() or folder==root:raise ValueError('需要项目内的原图文件夹')
    files=[p for p in folder.rglob('*') if p.is_file() and not p.is_symlink()]
    if not files:raise ValueError('文件夹里没有可清理的文件')
    log=[]
    for p in files:
        r=rel(project,p);digest=w.sha(p)
        for step in s['steps'].values():
            for a in step['artifacts']:
                if a['path']==r and a['sha256']==digest:a['purged_at']=w.now()
        p.unlink();log.append({'path':r,'sha256':digest})
    s['history'].append({'at':w.now(),'action':'purge-sources','files':log})
    w.save(project,s)
    return log

# ---------- 试跑：不走订单流程，只测角色一致性 ----------
def contact_sheet(images,dest,cols=4,cell=360):
    from PIL import Image
    rows=(len(images)+cols-1)//cols;sheet=Image.new('RGB',(cols*cell,rows*cell),(250,248,244))
    for i,path in enumerate(images):
        with Image.open(path) as im:
            im=im.convert('RGB');im.thumbnail((cell-12,cell-12))
            sheet.paste(im,((i%cols)*cell+(cell-im.width)//2,(i//cols)*cell+(cell-im.height)//2))
    sheet.save(dest,quality=90);return dest

def trial(outdir,people,style,scenes,videos,ratio,authorized):
    if not authorized:raise ValueError('试跑会产生生图/视频费用，需加 --authorized')
    out=Path(outdir).resolve()
    # 同一文件夹重跑时续做：已生成的图直接沿用，不重复付费；想重新生成就换新文件夹名。
    style_text=Path(style).read_text(encoding='utf-8').strip()
    lines=[x.strip().lstrip('-0123456789.、 ').strip() for x in Path(scenes).read_text(encoding='utf-8').splitlines()]
    lines=[x for x in lines if x and not x.startswith('#')][:12]
    if not style_text or not lines:raise ValueError('画风文件或场景清单为空')
    if ratio not in SIZES:raise ValueError('画幅仅支持 '+'、'.join(SIZES))
    if not 0<=videos<=len(lines) or videos>3:raise ValueError('--videos 取 0–3，先少量试')
    parsed=[]
    for item in people:
        name,_,files=item.partition('=')
        photos=[Path(x).resolve() for x in files.split(',') if x]
        if not name.strip() or not photos:raise ValueError('--person 格式：名字=照片1,照片2')
        for ph in photos:check_image(ph)
        parsed.append((name.strip(),photos))
    for d in (out/'characters',out/'shots'):d.mkdir(parents=True,exist_ok=True)
    sheets=[]
    for i,(name,photos) in enumerate(parsed,1):
        dest=out/'characters'/f'P{i}.png'
        if dest.exists():
            check_image(dest);sheets.append((name,dest));print('沿用已生成的角色设定图：',dest,flush=True);continue
        prompt=(f'{style_text}\n\n任务：根据参考照片，为「{name}」生成一张Q版角色设定图。同一画面包含正面、四分之三侧面、全身三个视角，'
                '浅色纯色背景，不加任何文字。严格按照片还原发型发色、脸型、五官特征和服饰；照片里没有的东西（如眼镜、帽子、饰品）一律不要添加；不要美化到认不出，'
                f'不改变年龄阶段和性别表现。所有参考图都是同一个人：{name}。')
        model=seedream(out,prompt,photos,'2048x2048',dest)
        sidecar(out,dest,{'kind':'trial-character','name':name,'model':model,'prompt':prompt})
        sheets.append((name,dest));print('角色设定图：',dest,flush=True)
    mapping='\n'.join(f'第{i}张参考图是第{i}个人「{n}」的Q版角色设定图，只用来保持此人外形、发型和服饰；画面里不要出现三视图排版。' for i,(n,_) in enumerate(sheets,1))
    shots=[]
    for i,scene in enumerate(lines,1):
        dest=out/'shots'/f'S{i:02}.png'
        if dest.exists():
            shots.append(dest);print(f'沿用已生成的分镜 {i}：',dest,flush=True);continue
        prompt=f'{style_text}\n\n{mapping}\n\n{scene}\n\n画幅 {ratio}，单一画面，不加文字。'
        model=seedream(out,prompt,[s for _,s in sheets],SIZES[ratio],dest)
        sidecar(out,dest,{'kind':'trial-keyframe','scene':scene,'model':model,'prompt':prompt})
        shots.append(dest);print(f'分镜 {i}/{len(lines)}：',dest,flush=True)
    contact_sheet([s for _,s in sheets]+shots,out/'总览.jpg')
    if videos:
        key,base=config();model=os.environ.get('ARK_VIDEO_MODEL',DEFAULT_VIDEO_MODEL);(out/'videos').mkdir(exist_ok=True)
        for i,pic in enumerate(shots[:videos],1):
            payload={'model':model,'content':[{'type':'text','text':lines[i-1]+'。动作自然连贯，一个主要动作，镜头缓慢推近 --duration 5 --ratio adaptive --resolution 720p --watermark false'},
                     {'type':'image_url','image_url':{'url':data_uri(pic)},'role':'first_frame'}]}
            if (out/'videos'/f'S{i:02}.mp4').exists():continue
            task=call('POST',base+'/contents/generations/tasks',key,payload).get('id')
            if not task:raise ValueError('未返回任务号')
            poll(out,task,out/'videos'/f'S{i:02}.mp4',key,base,10,900,{'kind':'trial-video','model':model,'first_frame':rel(out,pic)})
            print(f'视频 {i}/{videos} 完成',flush=True)
    return out/'总览.jpg'

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='cmd',required=True)
    q=sub.add_parser('character');q.add_argument('project',type=Path);q.add_argument('--name',required=True)
    q.add_argument('--photos',nargs='+',required=True);q.add_argument('--style',required=True);q.add_argument('--out',required=True)
    q.add_argument('--seed',type=int);q.add_argument('--outfit',help='设定图统一服装，如：简单的白色T恤和牛仔裤');q.add_argument('--authorized',action='store_true')
    q=sub.add_parser('keyframe');q.add_argument('project',type=Path);q.add_argument('--shot',required=True)
    q.add_argument('--prompt',required=True);q.add_argument('--characters',default='CHARACTERS.json');q.add_argument('--refs',nargs='+',required=True)
    q.add_argument('--out',required=True);q.add_argument('--ratio',default='16:9');q.add_argument('--seed',type=int);q.add_argument('--authorized',action='store_true')
    for name in ('i2v','i2v-batch'):
        q=sub.add_parser(name);q.add_argument('project',type=Path);q.add_argument('--manifest',default='VIDEO_PACK.json')
        q.add_argument('--duration',type=int,default=5);q.add_argument('--ratio',default='adaptive');q.add_argument('--resolution',default='720p')
        q.add_argument('--timeout',type=int,default=900);q.add_argument('--authorized',action='store_true')
        if name=='i2v':q.add_argument('--shot',required=True);q.add_argument('--out',required=True)
        else:q.add_argument('--outdir',default='videos');q.add_argument('--version',default='v1');q.add_argument('--max-shots',type=int,required=True)
    q=sub.add_parser('fetch');q.add_argument('project',type=Path);q.add_argument('--task',required=True);q.add_argument('--out',required=True)
    q=sub.add_parser('purge');q.add_argument('project',type=Path);q.add_argument('--dir',required=True);q.add_argument('--confirm',action='store_true')
    q=sub.add_parser('trial',help='不走订单流程，只测角色一致性');q.add_argument('outdir')
    q.add_argument('--person',action='append',required=True,help='名字=照片1,照片2，可重复');q.add_argument('--style',required=True)
    q.add_argument('--scenes',required=True);q.add_argument('--videos',type=int,default=0);q.add_argument('--ratio',default='16:9');q.add_argument('--authorized',action='store_true')
    a=p.parse_args()
    if a.cmd=='trial':r=trial(a.outdir,a.person,a.style,a.scenes,a.videos,a.ratio,a.authorized)
    elif a.cmd=='character':r=character(a.project,a.name,a.photos,a.style,a.out,a.authorized,a.seed,a.outfit)
    elif a.cmd=='keyframe':r=keyframe(a.project,a.shot,a.prompt,a.characters,a.refs,a.out,a.ratio,a.authorized,a.seed)
    elif a.cmd=='i2v':r=i2v(a.project,a.manifest,a.shot,a.out,a.duration,a.ratio,a.resolution,a.authorized,timeout=a.timeout)
    elif a.cmd=='i2v-batch':r=json.dumps(i2v_batch(a.project,a.manifest,a.outdir,a.version,a.max_shots,a.duration,a.ratio,a.resolution,a.authorized,timeout=a.timeout),ensure_ascii=False)
    elif a.cmd=='fetch':r=fetch(a.project,a.task,a.out)
    else:r=json.dumps(purge(a.project,a.dir,a.confirm),ensure_ascii=False,indent=2)
    print(r)

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,TypeError) as e:print('未完成：'+str(e),file=sys.stderr);sys.exit(2)
