import json
import io
import logging
import random
from functools import partial

import webdataset as wds

from PIL import Image


# IMPORTANT fitz aka PyMuPDF is AGPL licensed w/ a commercial purchased option
# manual intervention required to use it.
_USE_AGPL_PYMUPDF = False

if _USE_AGPL_PYMUPDF:
    try:
        import fitz
    except ImportError as e:
        fitz = None
else:
    fitz = None

from .loader import log_and_continue

_logger = logging.getLogger(__name__)


def filter_no_annotation_or_no_image(sample):
    # FIXME check sample for valid doc/image and annotation
    return True


class DocProcessor:

    def __init__(
            self,
            image_preprocess,
            anno_preprocess,
            image_key='tif;tiff;png',
            image_fmt='L',
            seed=0,
    ):
        self.image_preprocess = image_preprocess
        self.anno_preprocess = anno_preprocess
        self.image_ext = image_key.split(';')
        self.image_fmt = image_fmt
        self.squeeze_pages = True
        self.generator = random.Random()
        self.generator.seed(seed)
        # FIXME note, should move to torchvision v2 annotations at some point, they should
        #  have a generator arg (eventually) which will make proper state restore possible

    def _decode_image_pages(
            self,
            sample,
            ext,
            page_indices,
            num_anno_pages,
    ):
        with io.BytesIO(sample[ext]) as b:
            image = Image.open(b)
            num_image_pages = getattr(image, 'n_frames', 1)
            if num_image_pages != num_anno_pages:
                _logger.warning(
                    f'Mismatch between num image and num annotation pages {num_image_pages} != {num_anno_pages}'
                    f' for sample {sample["__url__"]}, {sample["__key__"]}.')
            pages = []
            for i, page_index in enumerate(page_indices):
                if num_image_pages > 1:
                    image.seek(page_index)
                else:
                    assert num_anno_pages == 1
                    image.load()

                if self.image_fmt:
                    image = image.convert(self.image_fmt)

                # if page_image_info is not None:
                #     # FIXME, if train objective involves masking or otherwise processing image
                #     #  with knowledge of annotations / text content, anno info should contain
                #     #  mask locations, etc. For such a task, we need to pass it to image preprocess
                #     image = self.image_preprocess(image, page_info=page_image_info[i])
                # else:
                image = self.image_preprocess(image)
            pages += [image]
        return image, num_image_pages

    def _decode_pdf_pages(
            self,
            sample,
            ext,
            page_indices,
            num_anno_pages,
    ):
        with io.BytesIO(sample[ext]) as b:
            # FIXME test and use an alternate pdf reader/render as default
            assert fitz is not None, "fitz (pymupdf) is not installed and enabled"
            doc = fitz.Document(stream=b)
            num_image_pages = doc.page_count
            pages = []
            for i, page_index in enumerate(page_indices):
                page = doc.load_page(page_index)
                pixmap = page.get_pixmap(dpi=150)
                image = Image.frombuffer('RGB', (pixmap.width, pixmap.height), pixmap.samples)

                if self.image_fmt:
                    image = image.convert(self.image_fmt)

                # if page_image_info is not None:
                #     # FIXME, if train objective involves masking or otherwise processing image
                #     #  with knowledge of annotations / text content, anno info should contain
                #     #  mask locations, etc. For such a task, we need to pass it to image preprocess
                #     image = self.image_preprocess(image, page_info=page_image_info[i])
                # else:
                image = self.image_preprocess(image)
            pages += [image]

        return pages, num_image_pages

    def __call__(self, sample):
        anno = json.loads(sample['json'])

        try:
            page_anno = self.anno_preprocess(anno, generator=self.generator)
        except Exception as exn:
            _logger.error(f'Issue processing annotation for {sample["__url__"]}, {sample["__key__"]}.')
            #_logger.error(json.dumps(anno, indent=4))
            raise(exn)

        info = None
        if isinstance(page_anno, tuple):
            page_anno, info = page_anno
            page_indices = info.get('page_indices', [0])  # the samples page indices
            num_decode_pages = len(page_indices)
            num_anno_pages = info.get('num_pages', 1)
            # page_image_info = info.get('image_info', None)
            # if page_image_info is not None:
            #     assert len(page_image_info) == len(page_indices)
        else:
            num_decode_pages = num_anno_pages = 1
            page_indices = [0]
            # page_image_info = None

        # decode page images
        page_images = []
        for ext in self.image_ext:
            if ext in sample:
                image_bytes = sample[ext]
                if ext == 'pdf':
                    images, num_image_pages = self._decode_pdf_pages(
                        sample,
                        ext,
                        page_indices,
                        num_anno_pages,
                    )
                else:
                    images, num_image_pages = self._decode_image_pages(
                        sample,
                        ext,
                        page_indices,
                        num_anno_pages,
                    )
                page_images.extend(images)
                # process one document type per doc, should be ordered by priority
                break

        assert len(page_images), 'No page images present'

        if self.squeeze_pages and num_decode_pages == 1:
            # FIXME always list?
            page_images = page_images[0]
            page_anno = {k: v[0] for k, v in page_anno.items()}

        decoded = dict(image=page_images, **page_anno)
        return decoded


def _decode_samples(
        data,
        decoder,
        handler=log_and_continue,
):
    """Decode samples with skip."""
    for sample in data:
        try:
            result = decoder(sample)
        except Exception as exn:
            if handler(exn):
                continue
            else:
                break

        # empty results are skipped
        if result is not None:
            if isinstance(sample, dict) and isinstance(result, dict):
                result["__key__"] = sample.get("__key__")
            yield result


def create_doc_anno_pipe(
    image_preprocess,
    anno_preprocess,
    # page_sampling='',
    image_key='tif;tiff;png',
    image_fmt='L',
    as_tuple=True,
):
    pipe = [
        wds.select(filter_no_annotation_or_no_image),
        # document decoding & pre-processing done together, there is coupling in random page
        # selection and possibly pre-processing / masking of image vs text
        partial(
            _decode_samples,
            decoder=DocProcessor(
                image_preprocess=image_preprocess,
                anno_preprocess=anno_preprocess,
                image_key=image_key,
                image_fmt=image_fmt,
            ),
        ),
    ]
    if as_tuple:
        pipe += [wds.to_tuple("image", "text", "target")]
    return pipe
