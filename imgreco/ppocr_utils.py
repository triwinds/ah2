from functools import lru_cache, cache
from typing import List

import numpy as np
import cv2
import textdistance
import logging

from rapidocr.ch_ppocr_rec import TextRecOutput
from rapidocr.utils.output import RapidOCROutput

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


@cache
def get_rapidocr():
    from rapidocr import RapidOCR
    return RapidOCR(params={"Global.log_level": "ERROR", "Global.use_det": True, "Global.use_rec": True})


@lru_cache(1)
def get_no_det_rapidocr():
    from rapidocr import RapidOCR
    return RapidOCR(params={"Global.log_level": "ERROR", "Global.use_det": False})


def ocr_for_single_line(img) -> str:
    """
    对单行文本进行OCR识别，返回识别的文本字符串

    Args:
        img: 图像数据，可以是 numpy array 或其他图像格式

    Returns:
        str: 识别的文本，如果识别失败返回空字符串
    """
    ocr_result = get_no_det_rapidocr()(img)
    if ocr_result and ocr_result.txts:
        return ocr_result.txts[0]
    return ''


def calc_box_center(box, scale=1):
    box_y = box[:, 1]
    box_x = box[:, 0]
    return int(np.average(box_x) * scale), int(np.average(box_y) * scale)


def detect_box(screen: Image, target_name: str, drop_score=0.3, box_thresh=0.1, unclip_ratio=1.6, no_scale=False) -> tuple[tuple[int, int] | None, float]:
    scale = 1 if no_scale else screen.height / 720
    if scale != 1:
        screen = screen.resize((screen.width / scale, 720))
    dbg_screen = screen.copy()

    # 使用 rapidocr 直接进行OCR识别
    ocr_result: RapidOCROutput = get_rapidocr()(screen.array, box_thresh=box_thresh, unclip_ratio=unclip_ratio)

    # 转换为 OcrResult 列表
    boxed_results = []
    if ocr_result and ocr_result.boxes is not None and len(ocr_result.boxes) > 0 and ocr_result.txts and ocr_result.scores:
        for box, text, score in zip(ocr_result.boxes, ocr_result.txts, ocr_result.scores):
            if score >= drop_score:
                boxed_results.append(OcrResult(text, score, box))

    max_score = 0
    max_res = None
    for res in boxed_results:
        # print(res.ocr_text)
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
