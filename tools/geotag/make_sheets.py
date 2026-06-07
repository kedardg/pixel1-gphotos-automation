#!/usr/bin/env python3
"""Build face-blurred contact sheets from a folder of photos, ordered by capture time.

Faces are detected with Haar cascades (frontal + profile + flipped profile) and
heavily blurred BEFORE anything is shown. Originals are never modified here.

Outputs:
  <out>/sheet_NNN.jpg   tiled grids with index labels
  <out>/index.json      [{idx, path, datetime, sheet, tile}] mapping
"""
import os, sys, json, subprocess, argparse
import cv2
import numpy as np

CASC = cv2.data.haarcascades
FRONTAL = cv2.CascadeClassifier(CASC + "haarcascade_frontalface_default.xml")
FRONTAL2 = cv2.CascadeClassifier(CASC + "haarcascade_frontalface_alt2.xml")
PROFILE = cv2.CascadeClassifier(CASC + "haarcascade_profileface.xml")

IMG_EXT = (".jpg", ".jpeg")


def list_images(folder):
    out = []
    for root, _, names in os.walk(folder):
        for n in names:
            if n.lower().endswith(IMG_EXT) and not n.startswith("._"):
                out.append(os.path.join(root, n))
    return out


def capture_times(paths):
    """Batch-read DateTimeOriginal via exiftool. Returns {path: 'YYYY:MM:DD HH:MM:SS' or ''}."""
    times = {}
    CH = 400
    for i in range(0, len(paths), CH):
        chunk = paths[i:i + CH]
        try:
            r = subprocess.run(
                ["exiftool", "-j", "-d", "%Y:%m:%d %H:%M:%S",
                 "-DateTimeOriginal", "-CreateDate", *chunk],
                capture_output=True, text=True)
            data = json.loads(r.stdout) if r.stdout.strip() else []
        except Exception:
            data = []
        for d in data:
            src = d.get("SourceFile")
            t = d.get("DateTimeOriginal") or d.get("CreateDate") or ""
            if src:
                times[os.path.abspath(src)] = t
    return times


def detect_faces(gray):
    boxes = []
    for casc in (FRONTAL, FRONTAL2, PROFILE):
        f = casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4,
                                  minSize=(24, 24))
        for (x, y, w, h) in f:
            boxes.append((x, y, w, h))
    # flipped for right-facing profiles
    flip = cv2.flip(gray, 1)
    W = gray.shape[1]
    f = PROFILE.detectMultiScale(flip, scaleFactor=1.1, minNeighbors=4,
                                 minSize=(24, 24))
    for (x, y, w, h) in f:
        boxes.append((W - x - w, y, w, h))
    return boxes


def blur_faces(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    H, W = img.shape[:2]
    for (x, y, w, h) in detect_faces(gray):
        # generous margin around detection
        mx, my = int(w * 0.45), int(h * 0.55)
        x0, y0 = max(0, x - mx), max(0, y - my)
        x1, y1 = min(W, x + w + mx), min(H, y + h + my)
        roi = img[y0:y1, x0:x1]
        if roi.size == 0:
            continue
        k = max(31, (((x1 - x0) // 2) | 1))
        img[y0:y1, x0:x1] = cv2.GaussianBlur(roi, (k, k), 0)
    return img


def label(img, text):
    cv2.rectangle(img, (0, 0), (len(text) * 16 + 12, 30), (0, 0, 0), -1)
    cv2.putText(img, text, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (0, 255, 255), 2, cv2.LINE_AA)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("out")
    ap.add_argument("--cols", type=int, default=5)
    ap.add_argument("--rows", type=int, default=6)
    ap.add_argument("--thumb", type=int, default=380)
    ap.add_argument("--detect-width", type=int, default=900,
                    help="downscale width used for face detection")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    paths = [os.path.abspath(p) for p in list_images(args.folder)]
    times = capture_times(paths)
    paths.sort(key=lambda p: (times.get(p, "9999") or "9999", p))

    per = args.cols * args.rows
    index = []
    sheet = []
    sheet_no = 0

    def flush():
        nonlocal sheet, sheet_no
        if not sheet:
            return
        tw = args.thumb
        th = int(tw * 2 / 3)
        grid = np.full((th * args.rows, tw * args.cols, 3), 30, np.uint8)
        for i, tile in enumerate(sheet):
            r, c = divmod(i, args.cols)
            grid[r*th:(r+1)*th, c*tw:(c+1)*tw] = tile
        outp = os.path.join(args.out, f"sheet_{sheet_no:03d}.jpg")
        cv2.imwrite(outp, grid, [cv2.IMWRITE_JPEG_QUALITY, 80])
        sheet = []
        sheet_no += 1

    for idx, p in enumerate(paths):
        img = cv2.imread(p)
        if img is None:
            continue
        # detect at moderate resolution
        H, W = img.shape[:2]
        scale = args.detect_width / W if W > args.detect_width else 1.0
        work = cv2.resize(img, (int(W*scale), int(H*scale))) if scale != 1.0 else img.copy()
        work = blur_faces(work)
        tw = args.thumb
        th = int(tw * 2 / 3)
        thumb = cv2.resize(work, (tw, th))
        label(thumb, str(idx))
        sheet.append(thumb)
        index.append({"idx": idx, "path": p, "datetime": times.get(p, ""),
                      "sheet": sheet_no, "tile": len(sheet) - 1})
        if len(sheet) == per:
            flush()
    flush()

    with open(os.path.join(args.out, "index.json"), "w") as f:
        json.dump(index, f, indent=0)
    print(f"{len(paths)} images -> {sheet_no} sheets in {args.out}")


if __name__ == "__main__":
    main()
