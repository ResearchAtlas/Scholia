"""Text recognition for scanned pages (slice-1 spec F3a step 2, sections 1, 7.1 and 15; S1-20).

One engine interface, so another platform adds an engine rather than a rewrite (section 1, Keeping
the port cheap). An engine has a version, which the PDF extractor's version names (readings are
shared by file and extractor version, so a new engine is a new reading), and recognize(bitmap),
which returns every line it finds, whatever its confidence, each with its box as fractions of the
image from its top left (the form passages' rectangles take), or raises Failed. An engine keeps
nothing between calls, so no state is shared across readings. engine() is the platform's: macOS
Vision through pyobjc-framework-Vision, or None where none loads, and scanned pages then wait for
OCR as they did before S1-20.

Recognition is local: nothing is sent anywhere. Recognized text and an engine's error description
(which can quote the page) are never logged or kept.
"""

import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Bitmap:
    """An 8-bit gray image: height rows of stride bytes each, the top row first."""
    pixels: bytes
    width: int
    height: int
    stride: int


@dataclass(frozen=True)
class Line:
    text: str
    box: tuple  # (left, top, right, bottom): fractions of the image from its top left
    confidence: float


class Failed(Exception):
    """The engine could not read a page."""


class Vision:
    """macOS Vision (section 15): VNRecognizeTextRequest's revision 3 at the accurate level, in Simplified
    Chinese and English, Chinese first (it is read only when it comes first). A page is read first
    without automatic language detection, which keeps every line's Han characters; a page in which
    that reading finds none is read again with detection, which reads English lines whole (with
    Chinese first and no detection they lose letters). Measured on synthetic pages: English lines 23
    of 40 exact without detection, 40 of 40 with it; with detection, Chinese lines holding two English
    terms lost all their Han characters in 7 of 60, and none without it. Text as small as
    MIN_TEXT_PIXELS is looked for: Vision may ignore text under its minimum height. Each setting is
    set, not left to the system's default. Where Vision cannot load, a page fails (Failed)."""

    version = "vision-3"
    LANGUAGES = ("zh-Hans", "en-US")
    MIN_TEXT_PIXELS = 12  # about 3 points at 300 dpi

    def recognize(self, bitmap):
        lines = self._read(bitmap, detect=False)
        if not any(_HAN.search(line.text) for line in lines):  # no Chinese on the page: its English read whole
            lines = self._read(bitmap, detect=True)
        return lines

    def _read(self, bitmap, detect):
        try:
            import Foundation
            import objc
            import Quartz
            import Vision as vision
        except ImportError:
            raise Failed() from None

        with objc.autorelease_pool():  # each reading's objects are let go as soon as it is done
            data = Foundation.NSData.dataWithBytes_length_(bitmap.pixels, len(bitmap.pixels))
            image = Quartz.CGImageCreate(bitmap.width, bitmap.height, 8, 8, bitmap.stride,
                                         Quartz.CGColorSpaceCreateDeviceGray(), Quartz.kCGImageAlphaNone,
                                         Quartz.CGDataProviderCreateWithCFData(data), None, False,
                                         Quartz.kCGRenderingIntentDefault)
            if image is None:
                raise Failed()
            request = vision.VNRecognizeTextRequest.alloc().init()
            request.setRevision_(vision.VNRecognizeTextRequestRevision3)
            request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
            request.setRecognitionLanguages_(list(self.LANGUAGES))
            request.setAutomaticallyDetectsLanguage_(detect)
            request.setMinimumTextHeight_(min(1.0, self.MIN_TEXT_PIXELS / bitmap.height))
            handler = vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
                image, Foundation.NSDictionary.dictionary())
            done, _ = handler.performRequests_error_([request], None)
            if not done:
                raise Failed()
            lines = []
            for observation in request.results() or ():
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue
                box = observation.boundingBox()  # from the image's bottom left
                left, bottom = box.origin.x, box.origin.y
                right, top = left + box.size.width, bottom + box.size.height
                lines.append(Line(str(candidates[0].string()),
                                  tuple(min(max(v, 0.0), 1.0) for v in (left, 1 - top, right, 1 - bottom)),
                                  float(candidates[0].confidence())))
            return lines


_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
VISION = Vision()


def engine():
    """The platform's engine, or None where there is none: Vision on macOS. It is the engine there even
    where its framework does not load (a damaged copy of the app): a scanned page then fails and can
    be tried again, rather than a reading by another extractor version replacing one made with it."""
    return VISION if sys.platform == "darwin" else None
