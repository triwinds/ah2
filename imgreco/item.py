from functools import lru_cache
from dataclasses import dataclass
from dataclasses_json import dataclass_json
from types import SimpleNamespace

import numpy as np
import cv2
import json
# from skimage.measure import compare_mse
from util import cvimage as Image, cvimage
import requests
import os
import logging
import app

from util.richlog import get_logger
from . import imgops
from . import minireco
from . import resources
from . import common
from imgreco.ppocr_utils import get_rapidocr


ocr_engine = get_rapidocr()
richlogger = get_logger(__name__)
logger = logging.getLogger(__name__)



def crop_item_middle_img(cv_item_img):
    # radius 60
    img_h, img_w = cv_item_img.shape[:2]
    ox, oy = img_w // 2, img_h // 2
    y1 = int(oy - 40)
    y2 = int(oy + 20)
    x1 = int(ox - 30)
    x2 = int(ox + 30)
    return cv_item_img[y1:y2, x1:x2]


def predict_item_dnn(cv_img, box_size=137):
    cv_img = cv2.resize(cv_img, (box_size, box_size))
    mid_img = crop_item_middle_img(cv_img)
    from .itemdb import load_net, dnn_items_by_class
    sess = load_net()
    input_name = sess.get_inputs()[0].name
    mid_img = np.moveaxis(mid_img, -1, 0)
    out = sess.run(None, {input_name: [mid_img.astype(np.float32)]})[0]

    # Get a class with a highest score.
    out = out.flatten()
    probs = common.softmax(out)
    classId = np.argmax(out)
    return probs[classId], dnn_items_by_class[classId]


@dataclass_json
@dataclass
class RecognizedItem:
    item_id: str
    name: str
    quantity: int
    low_confidence: bool = False
    item_type: str = None


@lru_cache(1)
def load_data():
    _, files = resources.get_entries('items')
    iconmats = {}
    itemmask = np.asarray(resources.load_image('common/itemmask.png', '1'))
    for filename in files:
        if filename.endswith('.png'):
            basename = filename[:-4]
            img = resources.load_image('items/' + filename, 'RGB')
            if img.size != (48, 48):
                img = img.resize((48, 48), Image.BILINEAR)
            mat = np.array(img)
            mat[itemmask] = 0
            iconmats[basename] = mat
    model = resources.load_pickle('minireco/NotoSansCJKsc-DemiLight-nums.dat')
    reco = minireco.MiniRecognizer(model, minireco.compare_ccoeff)
    return SimpleNamespace(itemmats=iconmats, num_recognizer=reco, itemmask=itemmask)


def all_known_items():
    from . import itemdb
    return itemdb.resources_known_items.keys()


def get_quantity_old(itemimg):
    numimg = imgops.scalecrop(itemimg, 0.39, 0.71, 0.82, 0.855).convert('L')
    numimg = imgops.crop_blackedge2(numimg, 120)
    if numimg is not None:
        numimg = imgops.clear_background(numimg, 120)
        numimg4legacy = numimg
        numimg = imgops.pad(numimg, 8, 0)
        numimg = imgops.invert_color(numimg)
        richlogger.logimage(numimg)
        from .ocr import acquire_engine_global_cached
        eng = acquire_engine_global_cached('zh-cn')
        from imgreco.ocr import OcrHint
        result = eng.recognize(numimg, char_whitelist='0123456789.万', tessedit_pageseg_mode='13',
                               hints=[OcrHint.SINGLE_LINE])
        qty_text = result.text
        richlogger.logtext(f'{qty_text=}')
        try:
            try:
                qty_base = float(qty_text.replace(' ', '').replace('万', ''))
            except:
                from . import itemdb
                qty_minireco, score = itemdb.num_recognizer.recognize2(numimg4legacy, subset='0123456789.万')
                richlogger.logtext(f'{qty_minireco=}, {score=}')
                if score > 0.2:
                    qty_text = qty_minireco
                    qty_base = float(qty_text.replace('万', ''))
            qty_scale = 10000 if '万' in qty_text else 1
            return int(qty_base * qty_scale)
        except:
            return None


def crop_blackedge(numimg: Image, threshold=None):
    if threshold is None:
        threshold = 110
    gap = int(numimg.height * 0.2)
    # thr_img = cvimage.fromarray(cv2.threshold(numimg.array, threshold, 255, cv2.THRESH_BINARY)[1], 'L')
    thr_img = numimg
    x_max = thr_img.array[2:-1, :].max(axis=0)
    left, right = 0, None
    i = thr_img.width
    if np.max(x_max[i-gap:i]) > threshold:
        right = i - np.argmax(x_max[0:i][::-1] > threshold) + int(gap/2)
        i = i - gap
    tmp_sum = np.sum(x_max[i-gap:i])
    while i > gap:
        # 从右往左找长度为 gap 的连续黑色像素
        tmp_sum += x_max[i-gap]
        if tmp_sum < threshold * gap:
            if right is None:
                right = i - np.argmax(x_max[0:i][::-1] > threshold) + int(gap/2)
                i = right - gap
            else:
                left = i - int(gap/2)
                break
        tmp_sum -= x_max[i]
        i -= 1
    if right is None:
        return imgops.crop_blackedge2(thr_img, 120)
    y_max = thr_img.array[:, left:right].max(axis=1)
    top = np.argmax(y_max > threshold)
    bottom = thr_img.height - max(0, np.argmax(y_max[::-1] > threshold) - 1)
    return numimg.crop((left, top, right, bottom))




def add_black_border(img: cv2.typing.MatLike, border_size=3):
    return cv2.copyMakeBorder(
        img,
        top=border_size,
        bottom=border_size,
        left=border_size,
        right=border_size,
        borderType=cv2.BORDER_CONSTANT,
        value=[0, 0, 0],  # BGR格式的黑色
    )


def crop_to_min_bounding_rect(image: cv2.typing.MatLike):
    """裁剪图像到包含所有轮廓的最小外接矩形"""
    # 转为灰度图（如果传入的是二值图，这个操作不会有问题）
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    # 寻找轮廓
    contours, _ = cv2.findContours(gray, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    # 如果没有找到轮廓就直接返回原图
    if not contours:
        return image
    # 合并所有轮廓点并获取外接矩形
    all_contours = np.vstack(contours)
    x, y, w, h = cv2.boundingRect(all_contours)
    # 裁剪图片并返回
    return image[y : y + h, x : x + w]


def preprocess(img: cv2.typing.MatLike):
    """彩色图像二值化处理，增强数字可见性"""
    # 检查图像是否为彩色
    if len(img.shape) == 2:
        # 如果是灰度图像，转换为三通道
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    # 创建较宽松的亮色阈值范围（包括浅灰、白色等亮色）
    # BGR格式
    lower_bright = np.array([180, 180, 180])
    upper_bright = np.array([255, 255, 255])

    # 基于颜色范围创建掩码
    bright_mask = cv2.inRange(img, lower_bright, upper_bright)

    # 进行形态学操作，增强文本可见性
    # 创建一个小的椭圆形核
    # kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (1, 1))
    # 膨胀操作，使文字更粗
    # dilated = cv2.dilate(bright_mask, kernel, iterations=1)
    # 闭操作，填充文字内的小空隙
    # closed = cv2.morphologyEx(dilated, cv2.MORPH_CLOSE, kernel)
    # closed = dilated
    closed = bright_mask

    # 去除细小噪声：过滤不够大的连通区域
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w <= 1:
            # 用黑色填充宽度小于等于1的区域
            cv2.drawContours(closed, [contour], -1, 0, thickness=cv2.FILLED)
        if h <= 13:
            # 用黑色填充高度小于等于13的区域
            cv2.drawContours(closed, [contour], -1, 0, thickness=cv2.FILLED)

    return closed


def do_num_ocr(numimg: Image):
    richlogger = get_logger(__name__)

    def to_ocr_input(image: Image):
        array = image.array
        if array.ndim == 2:
            return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
        if array.ndim == 3 and array.shape[2] == 1:
            return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
        if image.mode == 'BGR':
            return array
        return image.convert('BGR').array

    richlogger.logimage(numimg)
    result = ocr_engine(to_ocr_input(numimg), use_det=False, use_cls=False, use_rec=True)
    if len(result.txts) > 1:
        richlogger.logtext(f'{result=}')
    if not result.txts or not result.scores or result.scores[0] < 0.95:
        processed = preprocess(numimg.array)  # 二值化预处理
        processed = crop_to_min_bounding_rect(processed)  # 去除多余黑框
        processed = add_black_border(processed, border_size=3)  # 加上3像素黑框
        numimg = crop_blackedge(Image.fromarray(processed))
        richlogger.logimage(numimg)
        result = ocr_engine(to_ocr_input(numimg), use_det=False, use_cls=False, use_rec=True)
        if len(result.txts) > 1:
            richlogger.logtext(f'{result=}')
    if not result.txts or not result.scores:
        return None

    text = result.txts[0]
    final_txt = ''
    for c in text:
        if c in '0123456789.万':
            final_txt += c
    richlogger.logtext(f"OCR: text: '{result.txts[0]}', final text: '{final_txt}', score: {result.scores[0]}")
    if result.scores[0] > 0.5:
        return _parse_qty_text(final_txt)


def get_quantity(itemimg, item_id=None):
    # richlogger = get_logger(__name__)
    numimg = imgops.scalecrop(itemimg, 0.40, 0.71, 0.86, 0.86).convert('L')
    return do_num_ocr(numimg)
    # # thr = 110 if item_id != '30024' else 120
    # numimg = crop_blackedge(numimg)
    # numimg = imgops.crop_blackedge2(numimg, 120)
    # if numimg is not None:
    #     numimg = imgops.clear_background(numimg, 120)
    #     numimg4legacy = numimg
    #     from . import itemdb
    #     richlogger.logimage(numimg4legacy)
    #     qty_minireco, score = itemdb.num_recognizer.recognize2(numimg4legacy, subset='0123456789.万')
    #     richlogger.logtext(f'{qty_minireco=}, {score=}')
    #     if score > 0.65:
    #         try:
    #             return _parse_qty_text(qty_minireco)
    #         except:
    #             pass
    #     numimg = imgops.pad(numimg, 4, 0)
    #     numimg = imgops.invert_color(numimg)
    #     richlogger.logimage(numimg)
    #     from .ocr import acquire_engine_global_cached
    #     eng = acquire_engine_global_cached('zh-cn')
    #     from imgreco.ocr import OcrHint
    #     result = eng.recognize(numimg, char_whitelist='0123456789.万', tessedit_pageseg_mode='13',
    #                            hints=[OcrHint.SINGLE_LINE])
    #     qty_text = result.text
    #     richlogger.logtext(f'{qty_text=}')
    #     try:
    #         return _parse_qty_text(qty_text)
    #     except:
    #         return None


def _parse_qty_text(qty_text):
    if not qty_text:
        return None
    qty_base = float(qty_text.replace(' ', '').replace('万', ''))
    qty_scale = 10000 if '万' in qty_text else 1
    return int(qty_base * qty_scale)


def tell_item(itemimg, with_quantity=True, learn_unrecognized=False) -> RecognizedItem:
    richlogger = get_logger(__name__)
    richlogger.logimage(itemimg)
    from . import itemdb
    # l, t, r, b = scaledwh(80, 146, 90, 28)
    # print(l/itemimg.width, t/itemimg.height, r/itemimg.width, b/itemimg.height)
    # numimg = itemimg.crop(scaledwh(80, 146, 90, 28)).convert('L')
    low_confidence = False
    prob, dnnitem = predict_item_dnn(itemimg.convert('BGR').array)
    item_id = dnnitem.item_id
    name = dnnitem.item_name
    item_type = dnnitem.item_type
    richlogger.logtext(f'dnn matched {dnnitem} with prob {prob}')
    quantity = None
    if prob < 0.5 or item_id == 'other':
# scale = 48/itemimg.height
        img4reco = np.array(itemimg.resize((48, 48), Image.BILINEAR).convert('RGB'))
        img4reco[itemdb.itemmask] = 0

        scores = []
        for name, templ in itemdb.itemmats.items():
            scores.append((name, imgops.compare_mse(img4reco, templ)))

        scores.sort(key=lambda x: x[1])
        itemname, score = scores[0]
        # maxmatch = max(scores, key=lambda x: x[1])
        richlogger.logtext(repr(scores[:5]))
        diffs = np.diff([a[1] for a in scores])
        item_type = None
        if score < 800 and np.any(diffs > 600):
            richlogger.logtext('matched %s with mse %f' % (itemname, score))
            name = itemname
            dnnitem = itemdb.dnn_items_by_item_name.get(itemname)
            if dnnitem is not None:
                item_id = dnnitem.item_id
        else:
            richlogger.logtext('no match')
            low_confidence = True
            item_id = None
            name = '未知物品'

    if item_id is None and learn_unrecognized:
        name = itemdb.add_item(itemimg)

    if with_quantity and item_id is not None and item_id != 'other':
        if item_id == '30024':
            quantity = get_quantity_old(itemimg)
        else:
            quantity = get_quantity(itemimg, item_id)
        # quantity = get_quantity_old(itemimg)

    return RecognizedItem(item_id, name, quantity, low_confidence, item_type)
