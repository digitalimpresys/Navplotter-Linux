#!/usr/bin/env python3
"""Pre-labels the frames recorded by the node (Telecamera -> Registra), so that labelling them is mostly fixing
boxes rather than drawing them all.

    pip install ultralytics
    python3 prelabel.py 20261019-201500/                  # with the COCO model: finds boats and people only
    python3 prelabel.py 20261019-201500/ -m runs/detect/train/weights/best.pt   # with our model, once there is one

It writes next to each frame a YOLO .txt with our classes (classes.json) and a classes.txt, the layout that
Roboflow, CVAT ("YOLO 1.1") and Label Studio import as a dataset with its labels. Frames that already have a .txt
are left alone (they may be already corrected), unless --overwrite. After the corrections the folder goes to
merge_datasets.py as it is.
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from merge_datasets import IMG_EXT, Classes  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folders", nargs="+")
    ap.add_argument("-m", "--model", default="yolov8x.pt", help="ultralytics model (default: the largest COCO one)")
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    from ultralytics import YOLO
    model = YOLO(a.model)
    classes = Classes()
    for folder in a.folders:
        with open(os.path.join(folder, "classes.txt"), "w") as f:
            f.write("\n".join(classes.names) + "\n")
        frames = sorted(p for p in glob.glob(os.path.join(folder, "*")) if p.lower().endswith(IMG_EXT))
        done = 0
        for img in frames:
            txt = os.path.splitext(img)[0] + ".txt"
            if os.path.exists(txt) and not a.overwrite:
                continue
            r = model.predict(img, conf=a.conf, verbose=False)[0]
            with open(txt, "w") as f:
                for c, box in zip(r.boxes.cls.tolist(), r.boxes.xywhn.tolist()):
                    i = classes.index(r.names[int(c)])
                    if i is not None:
                        f.write("%d %.6f %.6f %.6f %.6f\n" % ((i,) + tuple(box)))
            done += 1
        print("%s: %d frames labelled, %d left as they were" % (folder, done, len(frames) - done))


if __name__ == "__main__":
    sys.exit(main())
