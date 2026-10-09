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
    """macOS Vision (section 15): VNRecognizeTextRequest's revision 3 at the accurate level, Simplified
    Chinese first, then English (Chinese is read only when it comes first). Text as small as
    MIN_TEXT_PIXELS is looked for: Vision may ignore text under its minimum height, which is set
    rather than left to the system's default."""

    version = "vision-3"
    LANGUAGES = ("zh-Hans", "en-US")
    MIN_TEXT_PIXELS = 12  # about 3 points at 300 dpi

    def recognize(self, bitmap):
        import Foundation
        import objc
        import Quartz
        import Vision as vision

        with objc.autorelease_pool():  # each page's objects are let go as soon as it is read
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


VISION = Vision()


def engine():
    """The platform's engine, or None: Vision on macOS, where its framework loads."""
    if sys.platform != "darwin":
        return None
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
    except ImportError:
        return None
    return VISION
