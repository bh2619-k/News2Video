import os
import argparse
import re
import math
import tempfile
import uuid
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, ImageFilter
from gtts import gTTS
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip, ColorClip

def fetch(url):
    h = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=h, timeout=30)
    r.raise_for_status()
    return r.text

def extract(url):
    html = fetch(url)
    s = BeautifulSoup(html, "html.parser")
    t = s.find("title").get_text(strip=True) if s.find("title") else "Article"
    a = s.find("article")
    ps = a.find_all("p") if a else s.select("div[itemprop='articleBody'] p")
    ps = [p for p in ps if p.find_parent("aside") is None]
    text = " ".join(p.get_text(" ", strip=True) for p in ps) or s.get_text(" ", strip=True)
    imgs = []
    scope = a if a else s
    is_yahoo = "yahoo.com" in urlparse(url).netloc.lower()

    def add(u):
        if not u:
            return
        if u.startswith("//"):
            u = "https:" + u
        u = urljoin(url, u)
        if u not in imgs:
            imgs.append(u)

    for im in scope.find_all("img"):
        if is_yahoo and im.find_parent("header") is not None:
            continue
        src = im.get("data-src") or im.get("data-original") or im.get("data-lazy-src") or im.get("src")
        if not src:
            srcset = im.get("data-srcset") or im.get("srcset")
            if srcset:
                parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
                if parts:
                    src = parts[-1]
        add(src)
    for pic in scope.find_all("source"):
        srcset = pic.get("srcset") or pic.get("data-srcset")
        if srcset:
            parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
            if parts:
                add(parts[-1])
    og = s.find("meta", attrs={"property": "og:image"}) or s.find("meta", attrs={"property": "og:image:secure_url"})
    tw = s.find("meta", attrs={"name": "twitter:image"}) or s.find("meta", attrs={"name": "twitter:image:src"})
    for m in (og, tw):
        if m and m.get("content"):
            add(m.get("content"))
    return t, text, imgs

def safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip()[:100] or "article"

def download_images(urls, outdir, limit):
    os.makedirs(outdir, exist_ok=True)
    saved = []
    for i, u in enumerate(urls[:limit]):
        ext = ".jpg"
        m = re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", u, re.I)
        if m:
            e = m.group(1).lower()
            ext = ".jpg" if e == "jpeg" or e == "jpg" else ".png" if e == "png" else ".webp"
        p = os.path.join(outdir, f"img_{i}{ext}")
        try:
            r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            with open(p, "wb") as f:
                f.write(r.content)
            saved.append(p)
        except:
            pass
    return saved

def to_rgb(path):
    im = Image.open(path)
    if im.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", im.size, (0, 0, 0))
        bg.paste(im, mask=im.split()[-1])
        return bg
    if im.mode != "RGB":
        return im.convert("RGB")
    return im

def prepare_images(img_paths, target_size, outdir):
    os.makedirs(outdir, exist_ok=True)
    out = []
    for p in img_paths:
        try:
            im = to_rgb(p)
            im = ImageOps.exif_transpose(im)
            im.thumbnail(target_size, Image.LANCZOS)
            bg = Image.new("RGB", target_size, (0, 0, 0))
            x = (target_size[0] - im.size[0]) // 2
            y = (target_size[1] - im.size[1]) // 2
            bg.paste(im, (x, y))
            op = os.path.join(outdir, os.path.basename(p) + ".jpg")
            bg.save(op, "JPEG", quality=90)
            out.append(op)
        except:
            pass
    return out

def make_backgrounds(img_paths, target_size, outdir):
    os.makedirs(outdir, exist_ok=True)
    out = []
    for p in img_paths:
        try:
            im = to_rgb(p)
            im = ImageOps.exif_transpose(im)
            tw, th = target_size
            iw, ih = im.size
            scale = max(tw / iw, th / ih)
            new_size = (int(iw * scale), int(ih * scale))
            cover = im.resize(new_size, Image.LANCZOS)
            x = (new_size[0] - tw) // 2
            y = (new_size[1] - th) // 2
            cover = cover.crop((x, y, x + tw, y + th))
            ratio = max(tw / iw, th / ih)
            radius = max(2, int(3 * ratio))
            cover = cover.filter(ImageFilter.GaussianBlur(radius=radius))
            op = os.path.join(outdir, os.path.basename(p) + ".bg.jpg")
            cover.save(op, "JPEG", quality=90)
            out.append(op)
        except:
            pass
    return out

def make_tts(text, lang, outpath):
    t = text.strip()
    if len(t) > 4500:
        t = t[:4500]
    g = gTTS(t, lang=lang)
    g.save(outpath)
    return outpath

def build_video(images, backgrounds, audio_path, fps, outpath, size):
    a = AudioFileClip(audio_path)
    d = a.duration
    n = max(1, len(images))
    per = d / n
    tw, th = size
    clips = []
    for idx, p in enumerate(images):
        bgp = backgrounds[idx] if idx < len(backgrounds) else None
        bg_clip = ImageClip(bgp).set_duration(per) if bgp else ColorClip(size, color=(0, 0, 0)).set_duration(per)
        try:
            iw, ih = Image.open(p).size
        except Exception:
            iw, ih = tw, th
        base = min(tw / iw, th / ih)

        def scale(t):
            return base * (1.03 + 0.03 * math.sin(2 * math.pi * t / per))

        fg_clip = ImageClip(p).set_duration(per).resize(scale).set_position("center")
        clip = CompositeVideoClip([bg_clip, fg_clip], size=size)
        clips.append(clip)
    v = concatenate_videoclips(clips, method="compose").set_audio(a)
    v.write_videofile(outpath, fps=fps, codec="libx264", audio_codec="aac", logger=None)
    a.close()
    v.close()
    return outpath

def run(url, lang, max_images, fps, size, outdir):
    t, txt, urls = extract(url)
    name = safe_name(t)
    base = os.path.join(outdir, name + "_" + uuid.uuid4().hex[:8])
    os.makedirs(base, exist_ok=True)
    rawdir = os.path.join(base, "raw")
    procdir = os.path.join(base, "processed")
    aud = os.path.join(base, "audio.mp3")
    txtfile = os.path.join(base, "text.txt")
    vid = os.path.join(base, name + ".mp4")

    with open(txtfile, "w", encoding="utf-8") as f:
        f.write(txt)

    dls = download_images(urls, rawdir, max_images)
    if not dls:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
            im = Image.new("RGB", size, (0, 0, 0))
            im.save(f.name, "JPEG")
            dls = [f.name]
    bgs = make_backgrounds(dls, size, procdir)
    if not bgs:
        bgs = []
    make_tts(txt, lang, aud)
    build_video(dls, bgs, aud, fps, vid, size)
    return vid

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--lang", default="en")
    p.add_argument("--max_images", type=int, default=15)
    p.add_argument("--fps", type=int, default=24)
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--outdir", default=os.path.join(os.getcwd(), "output"))
    a = p.parse_args()
    v = run(a.url, a.lang, a.max_images, a.fps, (a.width, a.height), a.outdir)
    print(v)

if __name__ == "__main__":
    os.system("cls & title News Video Bot ^| hiddenexe")
    main()
