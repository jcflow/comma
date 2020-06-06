import codecs
import collections
import csv
import io
import os
import pathlib
import typing
import urllib, urllib.parse
import zipfile

try:
    import requests
except ImportError:
    requests = None

import comma.extras


__author__ = "Jérémie Lumbroso <lumbroso@cs.princeton.edu>"

__all__ = [
    "MAX_SAMPLE_CHUNKSIZE",
    "URI_SCHEME_LOCAL",
    "URI_SCHEMES_ACCEPTED",

    "SourceType",

    "is_local",
    "is_url",
    "detect_line_terminator",
    "open_stream",

    "multislice_sequence",
]


MAX_SAMPLE_CHUNKSIZE = 10000

URI_SCHEME_LOCAL = "file"

URI_SCHEMES_ACCEPTED = ["http", "https"]

LINE_TERMINATORS = ["\r\n", "\r", "\n"]

LINE_TERMINATOR_DEFAULT = "\n"


# our type hint for a data source:
#  - a location (URL or file path), or string data
#  - a stream (text or binary)
#  - a string of bytes

SourceType = typing.Union[typing.AnyStr, typing.IO, bytes]

# type definitions for helper dictionaries
# (commented out for the moment because only Python 3.8+ compatible)

# class CommaInfoParams(typing.TypedDict):
#     dialect:         typing.Any
#     simple_dialect:  typing.Any
#     has_header:      bool
#     line_terminator: str
#
# class CommaInfo(typing.TypedDict):
#
#     # the parsed CSV rows
#     rows:   typing.List[typing.List[str]]
#
#     # a raw sample of the original file
#     sample: str
#
#     # an identifier for the source (if not raw buffer)
#     source: str
#
#     # CSV parameters
#     header: typing.Optional[typing.List[str]]
#     params: CommaInfoParams


def is_anystr(obj: typing.Union[typing.Any, typing.AnyStr]) -> bool:
    """
    Returns `True` if the `obj` object is of type `typing.AnyStr`.
    """
    return (
            obj is not None and
            (isinstance(obj, str) or
             isinstance(obj, bytes)))


def is_local(location: typing.AnyStr) -> typing.Optional[str]:
    """
    Detects whether a string location is a local file path.
    """
    
    # Eliminate obvious non-paths
    if location is None or location == "":
        return
    
    # Try to parse the location using urlparse (to handle file://)
    parsed_location = None
    
    try:        
        parsed_location = urllib.parse.urlparse(location)
        
        # parsed_location = ParseResult(
        #   scheme=..., netloc=..., path=...,
        #   params=..., query=..., fragment=...)

    except AttributeError:
        # May not be a string; regular paths should be parsed without
        # trouble
        return
    
    if parsed_location is None:
        return
        
    path = None
    
    if parsed_location.scheme in [URI_SCHEME_LOCAL, ""]:
        
        parsed_path = parsed_location.path
        
        if parsed_location.netloc != "":
            parsed_path = parsed_location.netloc + parsed_path
        
        if os.path.exists(parsed_path):
            path = parsed_path
        
        if os.path.exists(os.path.expanduser(parsed_path)):
            path = os.path.expanduser(parsed_path)
    
    if os.path.exists(location):
        path = location
    
    elif os.path.exists(os.path.expanduser(location)):
        path = os.path.expanduser(location)
    
    if path is not None:
        path = os.path.abspath(path)
        return path
    
    return


def is_url(location: str, no_request: bool = False) -> bool:
    """
    Detects whether a string location is a URL; may make a test HEAD request
    if the location is likely to be an actual URL (this behavior can be
    deactivated by setting `no_request` to `True`).
    """
    
    # Eliminate obvious non-URL
    if location is None or location == "":
        return False
    
    # Try to parse the URL using urlparse
    parsed_location = None
    
    try:        
        parsed_location = urllib.parse.urlparse(location)
        
        # parsed_location = ParseResult(
        #   scheme=..., netloc=..., path=...,
        #   params=..., query=..., fragment=...)

    except AttributeError:
        # May not be a string
        return False
    
    except Exception as exc:
        # Unexpected error
        return False
    
    if parsed_location is None:
        return False
    
    # Check parsed location
    if parsed_location.scheme == "" or parsed_location.netloc == "":
        return False
    
    # This is an actual file
    if parsed_location.scheme == URI_SCHEME_LOCAL:
        return False
    
    # If we cannot make an actual HEAD request, then this is 
    if no_request or requests is None:
        return parsed_location.scheme in URI_SCHEMES_ACCEPTED
    
    response = None
    
    # Try to make a HEAD request on the location to see if it is successful
    try:
        response = requests.head(location, allow_redirects=True, timeout=10)
    
    except requests.exceptions.InvalidSchema:
        # Not a supported scheme
        return False
    
    except requests.exceptions.ConnectionError:
        # Not able to connect
        return False
    
    if response is None:
        return False
    
    return response.ok


def detect_line_terminator(
        sample: typing.Optional[typing.AnyStr],
        default: typing.Optional[typing.AnyStr] = None
) -> str:
    """
    Detects the most likely line terminator (from `\r`, `\n`, `\r\n`), given
    a sample string, by counting the occurrences of each pattern and finding
    the longest and most frequent.
    """

    # update default
    if default is None:
        default = LINE_TERMINATOR_DEFAULT

    if sample is None or not hasattr(sample, "count"):
        return default

    # the sorting of options is made taking into account both
    # the number of occurrences of a pattern, and the length of
    # the pattern (this is so when "\r\n" occurs, it also boosts
    # the count of "\r" and "\n", so we must also look at the
    # LONGEST pattern with the best number of occurrences)

    ranked_options = sorted(
        zip(
            map(sample.count, LINE_TERMINATORS),  # counts
            map(len, LINE_TERMINATORS),           # length
            LINE_TERMINATORS),                    # line terminators
        reverse=True)

    best_option = ranked_options[0]

    # if the best option has been counted 0 times, means no line terminators
    # were found
    if best_option[0] == 0:
        return default

    # otherwise return the value of the best option
    return best_option[2]


def open_stream(
    source: SourceType,
    encoding: str = None,
    no_request: bool = False
) -> typing.Optional[typing.TextIO]:
    """
    Returns a seekable stream for text data that is properly decoded
    and ready to be read: The `source` can be actual data, a local file
    path, or a URL; it is possible to provide a stream that is compressed
    using ZIP. (This method will store all the data in memory.)
    """
    
    if source is None:
        return

    # local variable to keep track of the (most accurate for the user)
    # caption of the source
    internal_name = None
    
    # is this a STRING?
    if type(source) is str:
        source = typing.cast(typing.AnyStr, source)
        
        # multiline?
        if "\n" in source:
            newline = "\n"
            if "\r\n" in source:
                newline = "\r\n"
            return io.StringIO(initial_value=source, newline=newline)
        
        internal_name = source
        
        # is this a FILE?
        local_path = is_local(location=source)
        if local_path is not None:
            source = open(local_path, mode="rb")
        
        # is this a URL?
        elif not no_request and is_url(location=source):
            
            response = requests.get(url=source, allow_redirects=True)
            
            if not response.ok:
                return None
            
            if encoding is None:
                encoding = response.encoding
            
            if encoding is not None:
                source = io.TextIOWrapper(response.content, encoding=encoding)
            else:
                source = io.BytesIO(response.content)
        
        else:
            return None

    # is this BYTES?
    if type(source) is bytes:
        source = typing.cast(bytes, source)
        source = io.BytesIO(source)

    # is this a STREAM?
    if hasattr(source, "seekable"):
        
        # is it not seekable? if so, make it seekable
        if not source.seekable():
            
            # if not, read in all the data
            data = source.read()
            
            if type(data) is str:
                source = io.StringIO(data)
            
            elif type(data) is bytes:
                source = io.BytesIO(data)
        
        # is it compressed? if so, unzip it
        if zipfile.is_zipfile(source):
            zipsource = zipfile.ZipFile(source, mode="r")
            
            names = zipsource.namelist()
            
            count_total = 0
            count_csv = 0
            
            csv_filename = None
            
            for name in names:
                count_total += 1
                if os.path.splitext(name)[1].lower() == ".csv":
                    count_csv += 1
                    csv_filename = name
            
            if count_total == 1:
                # if only one file, we don't care if it is a CSV (we assume)
                data = zipsource.read(name=names[0])
                source = io.BytesIO(data)
            
            elif count_total > 1 and count_csv == 1:
                # if exactly one CSV, we know what to do
                data = zipsource.read(name=csv_filename)
                source = io.BytesIO(data)
            
            elif count_total == 0:
                raise ValueError(
                    "it seems the provided source is ZIP compressed; but "
                    "there are unknown issues unzipping it (or the archive "
                    "is empty)")
                
            else:
                # other situations are unclear
                raise ValueError(
                    "provided ZIP source is ambiguous, "
                    "contains multiple files: {}".format(names))
    
    # if at this point, has not been converted to stream, error
    if not hasattr(source, "seekable"):
        return None
    
    # look at a sample and analyze
    source.seek(0)
    sample = source.read(MAX_SAMPLE_CHUNKSIZE)
    
    # detect encoding if bytestring
    if type(sample) is bytes:
        if encoding is None:
            encoding = comma.extras.detect_encoding(sample)
        source = io.TextIOWrapper(source, encoding=encoding)
    
    # try to add useful metadata
    if internal_name is not None:
        try:
            source.buffer.name = internal_name
        except AttributeError:
            pass
    
    return source


def multislice_sequence(
        sequence: typing.Sequence[typing.Any],
        slice_list: typing.List[slice] = None,
) -> typing.Sequence[typing.Any]:
    """
    Returns the sub-sequence obtained from sequentially slicing the
    sequence `sequence` according to the series of slices in `slice_list`.
    """
    new_sequence = sequence

    if slice_list is not None:
        for sl in slice_list:
            new_sequence = new_sequence.__getitem__(sl)

    return new_sequence


def comment_stripper(stream: typing.TextIO, comment_line_chars: str = '#;'):
    for line in stream.readlines():
        
        stripped_line = line.strip()
        if not stripped_line:
            # empty lines
            continue

        # look at first character
        if stripped_line[:1] in comment_line_chars:
            # lines that are commented out
            continue

        # yield line
        yield line
