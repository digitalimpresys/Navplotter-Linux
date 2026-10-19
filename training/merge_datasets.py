#!/usr/bin/env python3
"""Merges marine datasets into one YOLO dataset with the NavPlotter classes (classes.json).

    python3 merge_datasets.py -o dataset  roboflow-buoys/  seadronessee/annotations/instances_train.json  registrazioni/

Each input is either
  - a YOLO dataset folder (Roboflow "YOLOv8" export, CVAT/Label Studio "YOLO" export, or ultralytics layout): the
    class names come from data.yaml, classes.txt or obj.names; the images are found anywhere under it and each
    one's labels are the .txt with the same name in the matching "labels" folder (or next to the image);
  - a COCO .json (SeaDronesSee, LaRS, Roboflow "COCO" export): the images are next to the .json, in an "images"
    folder next to it, or in the folder given with --images.
The class names of every input are mapped to ours through the synonyms of classes.json (case and "_"/"-" do not
matter); objects of any other class are dropped and listed at the end. Images with no objects are kept as
"background" (sea with nothing on it teaches the model not to see boats in waves), unless --no-empty.

The output is dataset/images/{train,val}, dataset/labels/{train,val} and dataset/data.yaml, ready for
ultralytics. Frames of the same recording (name-00001.jpg, name-00002.jpg ...) go all in train or all in val,
since nearly identical frames on both sides would make the validation look better than it is.
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def norm(name):
    return re.sub(r"[\s_\-]+", " ", str(name).strip().lower())


class Classes:
    def __init__(self, path=os.path.join(HERE, "classes.json")):
        with open(path) as f:
            j = json.load(f)
        self.names = j["names"]
        self.json = {k: j[k] for k in ("names", "it", "important") if k in j}
        self.map = {}
        for i, n in enumerate(self.names):
            for s in [n] + j.get("synonyms", {}).get(n, []):
                self.map[norm(s)] = i

    def index(self, name):
        return self.map.get(norm(name))


def read_names(folder):
    """Class names of a YOLO dataset folder, as a list (index -> name)."""
    for f in ("data.yaml", "data.yml", "dataset.yaml"):
        p = os.path.join(folder, f)
        if os.path.exists(p):
            return yaml_names(open(p).read())
    for f in ("classes.txt", "obj.names"):
        p = os.path.join(folder, f)
        if os.path.exists(p):
            return [l.strip() for l in open(p) if l.strip()]
    raise ValueError("%s: no data.yaml, classes.txt or obj.names" % folder)


def yaml_names(text):
    """The "names" of a data.yaml: a [list], a block list, or a block of index: name. PyYAML is not needed."""
    try:
        import yaml
        names = yaml.safe_load(text)["names"]
        return [names[k] for k in sorted(names)] if isinstance(names, dict) else list(names)
    except ImportError:
        pass
    m = re.search(r"^names:\s*\[(.*?)\]", text, re.M | re.S)
    if m:
        return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]
    m = re.search(r"^names:\s*\n((?:[ \t]+.*\n?)+)", text, re.M)
    if not m:
        raise ValueError("data.yaml without names")
    names = {}
    for i, line in enumerate(l.strip() for l in m.group(1).splitlines() if l.strip()):
        if line.startswith("- "):
            names[i] = line[2:].strip().strip("'\"")
        else:
            k, v = line.split(":", 1)
            names[int(k)] = v.strip().strip("'\"")
    return [names[k] for k in sorted(names)]


def label_path(img):
    """The YOLO .txt of an image: .../images/x.jpg -> .../labels/x.txt (the last "images" in the path), or next
    to the image."""
    parts = img.split(os.sep)
    base = os.path.splitext(parts[-1])[0] + ".txt"
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == "images":
            p = os.sep.join(parts[:i] + ["labels"] + parts[i + 1:-1] + [base])
            if os.path.exists(p):
                return p
    return os.path.join(os.path.dirname(img), base)


def read_yolo(folder, classes, dropped):
    names = read_names(folder)
    for img in sorted(glob.glob(os.path.join(folder, "**", "*"), recursive=True)):
        if not img.lower().endswith(IMG_EXT):
            continue
        rows, lp = [], label_path(img)
        if os.path.exists(lp):
            for line in open(lp):
                v = line.split()
                if len(v) != 5:          # polygons (segmentation exports) are skipped: boxes only
                    continue
                c = int(float(v[0]))
                name = names[c] if c < len(names) else str(c)
                i = classes.index(name)
                if i is None:
                    dropped[name] += 1
                else:
                    rows.append((i,) + tuple(float(x) for x in v[1:]))
        yield img, rows


def read_coco(path, classes, dropped, images_dir=None):
    with open(path) as f:
        j = json.load(f)
    cats = {c["id"]: c["name"] for c in j.get("categories", [])}
    anns = collections.defaultdict(list)
    for a in j.get("annotations", []):
        anns[a["image_id"]].append(a)
    here = os.path.dirname(os.path.abspath(path))
    dirs = [images_dir] if images_dir else [here, os.path.join(here, "images"), os.path.dirname(here),
                                            os.path.join(os.path.dirname(here), "images")]
    for im in j.get("images", []):
        img = next((p for p in (os.path.join(d, im["file_name"]) for d in dirs)
                    if os.path.exists(p)), None)
        if not img:
            img = next((p for p in (os.path.join(d, os.path.basename(im["file_name"])) for d in dirs)
                        if os.path.exists(p)), None)
        if not img:
            dropped["(missing image)"] += 1
            continue
        W, H = float(im["width"]), float(im["height"])
        rows = []
        for a in anns[im["id"]]:
            name = cats.get(a["category_id"], str(a["category_id"]))
            i = classes.index(name)
            if i is None:
                dropped[name] += 1
                continue
            x, y, w, h = a["bbox"]
            if w <= 0 or h <= 0:
                continue
            rows.append((i, (x + w / 2) / W, (y + h / 2) / H, w / W, h / H))
        yield img, rows


def group(img):
    """Frames of one recording share a group: name-00012.jpg -> name."""
    base = os.path.splitext(os.path.basename(img))[0]
    return os.path.dirname(img) + "/" + re.sub(r"[-_]\d{3,}$", "", base)


def is_val(img, val):
    return int(hashlib.md5(group(img).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < val


def merge(inputs, out, classes, val=0.15, keep_empty=True, images_dir=None, log=print):
    for s in ("train", "val"):
        os.makedirs(os.path.join(out, "images", s), exist_ok=True)
        os.makedirs(os.path.join(out, "labels", s), exist_ok=True)
    dropped = collections.Counter()
    counts = collections.Counter()
    images = collections.Counter()
    for n, src in enumerate(inputs):
        src_name = os.path.splitext(os.path.normpath(src))[0]      # sds/ann.json -> 00sdsann
        tag = "%02d%s" % (n, re.sub(r"[^A-Za-z0-9]+", "", "-".join(src_name.split(os.sep)[-2:]))[:24])
        items = read_coco(src, classes, dropped, images_dir) if src.endswith(".json") else read_yolo(src, classes, dropped)
        for img, rows in items:
            if not rows and not keep_empty:
                continue
            split = "val" if is_val(img, val) else "train"
            name = tag + "_" + os.path.basename(img)
            dst = os.path.join(out, "images", split, name)
            if not os.path.exists(dst):
                try:
                    os.link(img, dst)
                except OSError:
                    shutil.copy2(img, dst)
            with open(os.path.join(out, "labels", split, os.path.splitext(name)[0] + ".txt"), "w") as f:
                for r in rows:
                    c, x, y, w, h = r
                    f.write("%d %.6f %.6f %.6f %.6f\n" % (c, min(max(x, 0), 1), min(max(y, 0), 1),
                                                          min(max(w, 0), 1), min(max(h, 0), 1)))
                    counts[classes.names[c]] += 1
            images[split] += 1
    with open(os.path.join(out, "data.yaml"), "w") as f:
        f.write("path: %s\ntrain: images/train\nval: images/val\nnames:\n" % os.path.abspath(out))
        for i, n in enumerate(classes.names):
            f.write("  %d: %s\n" % (i, n))
    with open(os.path.join(out, "classes.json"), "w") as f:
        json.dump(classes.json, f, ensure_ascii=False, indent=1)
    log("Images: %d train, %d val" % (images["train"], images["val"]))
    for n in classes.names:
        log("  %-10s %d" % (n, counts[n]))
    if dropped:
        log("Dropped (not one of our classes; add them to the synonyms in classes.json if they are):")
        for n, c in dropped.most_common():
            log("  %-24s %d" % (n, c))
    return images, counts, dropped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("inputs", nargs="+", help="YOLO dataset folders and/or COCO .json files")
    ap.add_argument("-o", "--out", default="dataset")
    ap.add_argument("--classes", default=os.path.join(HERE, "classes.json"))
    ap.add_argument("--val", type=float, default=0.15, help="share of the images for validation")
    ap.add_argument("--images", help="images folder of the COCO .json inputs, when not next to them")
    ap.add_argument("--no-empty", action="store_true", help="drop the images with no objects")
    a = ap.parse_args(argv)
    merge(a.inputs, a.out, Classes(a.classes), a.val, not a.no_empty, a.images)


if __name__ == "__main__":
    sys.exit(main())
