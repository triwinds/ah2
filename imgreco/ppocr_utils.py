from functools import lru_cache

import cv2
import logging
import numpy as np
import textdistance

from util.cvimage import Image
from util.richlog import get_logger


logger = logging.getLogger(__name__)
richlogger = get_logger(__name__)


class OcrResult:
    def __init__(self, ocr_text, score, box):
        self.ocr_text = ocr_text
        self.score = score
        self.box = box

    def __str__(self):
        return f'OcrResult({self.ocr_text}, {self.score}, {self.box})'

    def __repr__(self):
        return self.__str__()


class PPOcrONNXOutput:
    def __init__(self, txts=None, scores=None, boxes=None):
        self.txts = txts or []
        self.scores = scores or []
        self.boxes = boxes


class PPOcrONNXAdapter:
    def __init__(self, ocr, default_use_det=True):
        self.ocr = ocr
        self.default_use_det = default_use_det

    def __call__(self, img, use_det=None, use_cls=True, use_rec=True, drop_score=0.5,
                 box_thresh=None, unclip_ratio=None, **kwargs):
        if use_det is None:
            use_det = self.default_use_det

        if not use_rec:
            return PPOcrONNXOutput(boxes=[] if use_det else None)

        if use_det:
            results = self.ocr.detect_and_ocr(
                img,
                drop_score=drop_score,
                box_thresh=box_thresh,
                unclip_ratio=unclip_ratio,
            )
            return PPOcrONNXOutput(
                txts=[result.ocr_text for result in results],
                scores=[result.score for result in results],
                boxes=[result.box for result in results],
            )

        result = self.ocr.ocr_single_line(img)
        if result:
            text, score = result
            return PPOcrONNXOutput(txts=[text], scores=[score], boxes=None)
        return PPOcrONNXOutput(boxes=None)

    def detect_and_ocr(self, img, drop_score=0.3, box_thresh=0.1, unclip_ratio=1.6):
        return self.ocr.detect_and_ocr(
            img,
            drop_score=drop_score,
            box_thresh=box_thresh,
            unclip_ratio=unclip_ratio,
        )

    def ocr_single_line(self, img):
        return self.ocr.ocr_single_line(img)

    def ocr_lines(self, img_list):
        results = []
        for img in img_list:
            result = self.ocr.ocr_single_line(img)
            results.append([result] if result else [])
        return results

    def set_char_whitelist(self, chars):
        self.ocr.set_char_whitelist(chars)


class RapidOCRAdapter:
    def __init__(self, ocr, default_use_det=True):
        self.ocr = ocr
        self.default_use_det = default_use_det

    def __call__(self, img, use_det=None, use_cls=True, use_rec=True, drop_score=0.5,
                 box_thresh=None, unclip_ratio=None, **kwargs):
        if use_det is None:
            use_det = self.default_use_det

        if not use_rec:
            return PPOcrONNXOutput(boxes=[] if use_det else None)

        result = self.ocr(
            img,
            use_det=use_det,
            use_cls=use_cls,
            use_rec=use_rec,
            text_score=drop_score,
            box_thresh=0.5 if box_thresh is None else box_thresh,
            unclip_ratio=1.6 if unclip_ratio is None else unclip_ratio,
        )
        return PPOcrONNXOutput(
            txts=list(result.txts or []),
            scores=list(result.scores or []),
            boxes=None if getattr(result, 'boxes', None) is None else list(result.boxes),
        )

    def detect_and_ocr(self, img, drop_score=0.3, box_thresh=0.1, unclip_ratio=1.6):
        result = self.ocr(
            img,
            use_det=True,
            use_cls=True,
            use_rec=True,
            text_score=drop_score,
            box_thresh=box_thresh,
            unclip_ratio=unclip_ratio,
        )
        if not result.txts or not result.scores or result.boxes is None:
            return []
        return [
            OcrResult(text, score, box)
            for text, score, box in zip(result.txts, result.scores, result.boxes)
        ]

    def ocr_single_line(self, img):
        result = self.ocr(img, use_det=False, use_cls=False, use_rec=True)
        if not result.txts or not result.scores:
            return None
        return result.txts[0], result.scores[0]

    def ocr_lines(self, img_list):
        results = []
        for img in img_list:
            result = self.ocr_single_line(img)
            results.append([result] if result else [])
        return results

    def set_char_whitelist(self, chars):
        # RapidOCR Python API does not expose runtime whitelist updates.
        return None


@lru_cache(1)
def get_ppocr():
    from ppocronnx.predict_system import TextSystem
    return TextSystem(box_thresh=0.1)


@lru_cache(1)
def get_rapidocr():
    from rapidocr import RapidOCR
    return RapidOCRAdapter(RapidOCR())


@lru_cache(1)
def get_no_det_rapidocr():
    from rapidocr import RapidOCR
    return RapidOCRAdapter(RapidOCR(), default_use_det=False)


def ocr_for_single_line(img) -> str:
    """
    对单行文本进行OCR识别，返回识别的文本字符串

    Args:
        img: 图像数据，可以是 numpy array 或其他图像格式

    Returns:
        str: 识别的文本，如果识别失败返回空字符串
    """
    result = get_ppocr().ocr_single_line(img)
    if result:
        return result[0]
    return ''


def calc_box_center(box, scale=1):
    box_y = box[:, 1]
    box_x = box[:, 0]
    return int(np.average(box_x) * scale), int(np.average(box_y) * scale)


def detect_box(screen: Image, target_name: str, drop_score=0.3, box_thresh=0.1, unclip_ratio=1.6, no_scale=False):
    scale = 1 if no_scale else screen.height / 720
    if scale != 1:
        screen = screen.resize((screen.width / scale, 720))
    dbg_screen = screen.copy()
    ppocr = get_ppocr()
    boxed_results = ppocr.detect_and_ocr(screen.array, drop_score=drop_score,
                                         box_thresh=box_thresh, unclip_ratio=unclip_ratio)
    max_score = 0
    max_res = None
    for res in boxed_results:
        cv2.drawContours(dbg_screen.array, [np.asarray(res.box, dtype=np.int32)], 0, (255, 0, 0), 2)
        richlogger.logtext(f'{res.ocr_text} {res.score} {res.box}')
        score = textdistance.sorensen(target_name, res.ocr_text)
        if score > max_score:
            max_score = score
            max_res = res
    if not max_res:
        return None, 0
    box_center = calc_box_center(max_res.box, scale)
    cv2.drawContours(dbg_screen.array, [np.asarray(max_res.box, dtype=np.int32)], 0, (0, 255, 0), 2)
    cv2.circle(dbg_screen.array, box_center, 4, (0, 0, 255), -1)
    richlogger.logimage(dbg_screen)
    richlogger.logtext(f"result {max_res}, box_center: {box_center}")
    logger.info(f"result {max_res}, box_center: {box_center}, score: {max_score:.3f}")
    return box_center, max_score
