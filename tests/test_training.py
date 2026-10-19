"""Tests of the dataset merge of the marine model (training/merge_datasets.py)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "training"))
import merge_datasets as M  # noqa: E402


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.classes = M.Classes()

    def tearDown(self):
        self.tmp.cleanup()

    def labels(self, out):
        rows = []
        for s in ("train", "val"):
            folder = os.path.join(out, "labels", s)
            for f in sorted(os.listdir(folder)):
                rows += [(f, l.split()[0]) for l in open(os.path.join(folder, f))]
        return rows

    def test_yaml_names(self):
        self.assertEqual(M.yaml_names("nc: 2\nnames: ['Boat', \"buoy\"]\n"), ["Boat", "buoy"])
        self.assertEqual(M.yaml_names("names:\n  - Boat\n  - buoy\n"), ["Boat", "buoy"])
        self.assertEqual(M.yaml_names("names:\n  0: Boat\n  1: buoy\ntrain: x\n"), ["Boat", "buoy"])

    def test_synonyms(self):
        c = self.classes
        self.assertEqual(c.names[c.index("Swimmer")], "person")
        self.assertEqual(c.names[c.index("life_saving_appliances")], "buoy")
        self.assertEqual(c.names[c.index("Jet-Ski")], "boat")
        self.assertIsNone(c.index("car"))

    def test_roboflow_coco_and_recording(self):
        rf = os.path.join(self.d, "rf")
        touch(rf + "/data.yaml", "names: ['Buoy', 'seagull']\n")
        touch(rf + "/train/images/a.jpg", "x")
        touch(rf + "/train/labels/a.txt", "0 0.5 0.5 0.1 0.1\n1 0.2 0.2 0.1 0.1\n")
        touch(rf + "/valid/images/b.jpg", "x")                      # no labels: background
        coco = os.path.join(self.d, "sds")
        touch(coco + "/images/c.jpg", "x")
        touch(coco + "/ann.json", json.dumps({
            "images": [{"id": 1, "file_name": "c.jpg", "width": 200, "height": 100},
                       {"id": 2, "file_name": "missing.jpg", "width": 1, "height": 1}],
            "categories": [{"id": 3, "name": "swimmer"}, {"id": 4, "name": "boat"}],
            "annotations": [{"image_id": 1, "category_id": 3, "bbox": [50, 25, 20, 10]},
                            {"image_id": 1, "category_id": 4, "bbox": [0, 0, 100, 50]}]}))
        rec = os.path.join(self.d, "rec")                          # a recording labelled with our classes
        touch(rec + "/classes.txt", "boat\nship\n")
        for i in range(1, 5):
            touch(rec + "/20261019-201500-%05d.jpg" % i, "x")
            touch(rec + "/20261019-201500-%05d.txt" % i, "1 0.5 0.5 0.3 0.2\n")
        out = os.path.join(self.d, "out")
        images, counts, dropped = M.merge([rf, coco + "/ann.json", rec], out, self.classes, log=lambda *a: None)
        self.assertEqual(images["train"] + images["val"], 7)
        self.assertEqual(dict(counts), {"buoy": 1, "person": 1, "boat": 1, "ship": 4})
        self.assertEqual(dropped["seagull"], 1)
        self.assertEqual(dropped["(missing image)"], 1)
        rows = dict((f, c) for f, c in self.labels(out) if "sds" in f and c == "3")
        self.assertTrue(rows)                                      # swimmer -> person (index 3)
        for s in ("train", "val"):
            for f in os.listdir(os.path.join(out, "labels", s)):
                for line in open(os.path.join(out, "labels", s, f)):
                    if "sds" in f and line.startswith("3 "):
                        self.assertEqual(line.split()[1:], ["0.300000", "0.300000", "0.100000", "0.100000"])
        # the frames of one recording are all on the same side
        sides = {s for s in ("train", "val") for f in os.listdir(os.path.join(out, "images", s)) if "rec" in f}
        self.assertEqual(len(sides), 1)
        self.assertIn("names:\n  0: boat\n", open(out + "/data.yaml").read())
        self.assertEqual(json.load(open(out + "/classes.json"))["it"]["buoy"], "boa")

    def test_no_empty(self):
        rf = os.path.join(self.d, "rf")
        touch(rf + "/classes.txt", "car\n")
        touch(rf + "/images/train/a.jpg", "x")
        touch(rf + "/labels/train/a.txt", "0 0.5 0.5 0.1 0.1\n")
        images, _, _ = M.merge([rf], os.path.join(self.d, "o"), self.classes, keep_empty=False, log=lambda *a: None)
        self.assertEqual(sum(images.values()), 0)


if __name__ == "__main__":
    unittest.main()
