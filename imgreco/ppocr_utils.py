from functools import lru_cache
from typing import List

import numpy as np
import cv2
import textdistance
import logging

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


class RapidOCRAdapter:
    """RapidOCR适配器，保持与ppocr的API兼容"""

    def __init__(self, ocr):
        self.ocr = ocr

    def detect_and_ocr(self, img, drop_score=0.3, box_thresh=0.1, unclip_ratio=1.6) -> List[OcrResult]:
        """适配detect_and_ocr方法"""
        ocr_result = self.ocr(img, box_thresh=box_thresh, unclip_ratio=unclip_ratio)

        results = []
        if ocr_result is None:
            return results
        if ocr_result.boxes is None:
            return results
        if ocr_result.txts is None:
            return results
        if ocr_result.scores is None:
            return results

        for box, text, score in zip(ocr_result.boxes, ocr_result.txts, ocr_result.scores):
            if score >= drop_score:
                results.append(OcrResult(text, score, box))
        return results

    def ocr_single_line(self, img):
        """适配ocr_single_line方法，返回字符串列表"""
        ocr_result = self.ocr(img)
        if ocr_result is None:
            return []

        # 返回元组列表
        res = []
        for text, score in zip(ocr_result.txts, ocr_result.scores):
            res.append((text, score))
        return res

    def ocr_lines(self, img_list):
        """适配ocr_lines方法，返回字符串列表的列表"""
        results = []
        for img in img_list:
            ocr_result = self.ocr(img)
            if ocr_result is None:
                results.append([])
                continue

            # 返回元组列表
            line_results = []
            for text, score in zip(ocr_result.txts, ocr_result.scores):
                line_results.append((text, score))
            results.append(line_results)
        return results


rapid_ocr = None


def get_rapidocr():
    from rapidocr import RapidOCR
    global rapid_ocr
    if rapid_ocr is None:
        rapid_ocr = RapidOCR()
    return rapid_ocr


@lru_cache(1)
def get_ppocr():
    return RapidOCRAdapter(get_rapidocr())


def calc_box_center(box, scale=1):
    box_y = box[:, 1]
    box_x = box[:, 0]
    return int(np.average(box_x) * scale), int(np.average(box_y) * scale)


def detect_box(screen: Image, target_name: str, drop_score=0.3, box_thresh=0.1, unclip_ratio=1.6, no_scale=False) -> tuple[tuple[int, int] | None, float]:
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
