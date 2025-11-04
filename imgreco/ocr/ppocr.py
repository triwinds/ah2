from util import cvimage
from .common import *
import cv2
import numpy as np
from functools import lru_cache
import logging
from . import OcrHint

is_online = False
# OCR 过程是否需要网络


info = "rapidocr"

from imgreco.ppocr_utils import get_rapidocr, get_no_det_rapidocr

ocr = get_rapidocr()


# 模块说明，用于在 log 中显示
def check_supported():
    """返回模块是否可用"""
    return True


class PaddleOcr(OcrEngine):
    def __init__(self, lang, **kwargs):
        super().__init__(lang, **kwargs)

    def recognize(self, image, ppi=70, hints=None, **kwargs):
        if image.mode != 'BGR':
            image = image.convert('BGR')
        cv_img = image.array
        single_line_flag = image.height < 35
        if hints is not None and OcrHint.SINGLE_LINE in hints:
            single_line_flag = True
        
        # RapidOCR doesn't support char_whitelist directly, so we'll ignore it for now
        # if 'char_whitelist' in kwargs:
        #     ocr.set_char_whitelist(kwargs['char_whitelist'])
        
        if single_line_flag:
            if image.height > image.width:
                cv_img = np.rot90(cv_img)
            # RapidOCR returns a RapidOCROutput object with attributes
            ocr_result = ocr(cv_img)
            texts = ocr_result.txts if ocr_result else []
            scores = ocr_result.scores if ocr_result else []
            logging.debug(f'PaddleOcr.recognize: {texts}')
            if texts and scores and scores[0] > 0.55:
                result = OcrResult([OcrLine([OcrWord(Rect(0, 0), w) for w in texts[0].strip()])])
            else:
                result = OcrResult([])
        else:
            # RapidOCR returns a RapidOCROutput object with attributes
            ocr_result = ocr(cv_img)
            texts = ocr_result.txts if ocr_result else []
            logging.debug(f'PaddleOcr.recognize: {texts}')
            if texts:
                line = [OcrLine([OcrWord(Rect(0, 0), w) for w in text]) for text in texts]
                result = OcrResult(line)
            else:
                result = OcrResult([])
        
        # RapidOCR doesn't support char_whitelist directly
        # if 'char_whitelist' in kwargs:
        #     ocr.set_char_whitelist(None)
        return result


def ocr_for_single_line(img, cand_alphabet: str = None):
    # RapidOCR doesn't support char_whitelist directly
    # if cand_alphabet:
    #     ocr.set_char_whitelist(cand_alphabet)
    
    # RapidOCR returns a RapidOCROutput object with attributes
    ocr_result = get_no_det_rapidocr()(img)
    texts = ocr_result.txts if ocr_result else []
    if texts:
        res = texts[0].strip()
    else:
        res = ''
    
    # RapidOCR doesn't support char_whitelist directly
    # if cand_alphabet:
    #     ocr.set_char_whitelist(None)
    return res


def do_ocr(img, cand_alphabet: str = None):
    # RapidOCR doesn't support char_whitelist directly
    # if cand_alphabet:
    #     ocr.set_char_whitelist(cand_alphabet)
    
    # RapidOCR returns a RapidOCROutput object with attributes
    ocr_result = ocr(img)
    texts = ocr_result.txts if ocr_result else []
    res = ''
    if texts:
        for text in texts:
            res += text
    res = res.strip()
    
    # RapidOCR doesn't support char_whitelist directly
    # if cand_alphabet:
    #     ocr.set_char_whitelist(None)
    return res


def search_in_list(s_list, x, min_score=0.5):
    import textdistance
    max_sim = -1
    res = None
    if (isinstance(s_list, set) or isinstance(s_list, map)) and x in s_list:
        return x, 1
    for s in s_list:
        if s == x:
            return x, 1
        sim = textdistance.sorensen(s, x)
        if sim > max_sim:
            max_sim = sim
            res = s
    if min_score <= max_sim:
        return res, max_sim


def ocr_and_correct(img, s_list, cand_alphabet: str = None, min_score=0.5, log_level=None):
    ocr_str = ocr_for_single_line(img, cand_alphabet)
    res = search_in_list(s_list, ocr_str, min_score)
    if log_level:
        logging.log(log_level, f'ocr_str, res: {ocr_str, res}')
    return res[0] if res else None


Engine = PaddleOcr
