#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import os
import re
import copy
import json
import uuid
from Queue import Queue, Empty
from . import util
from .const import *

index_re = re.compile('.+/(\d+)/([^\/]+)/*')
gallery_re = re.compile('/([a-f0-9]{10})/[^\-]+\-(\d+)')
imghash_re = re.compile('/h/([a-f0-9]{40})')
fullimg_re = re.compile('fullimg.php\?gid=([a-z0-9]+)&page=(\d+)&key=')

class Task(object):
    def __init__(self, url, cfgdict):
        self.url = url
        if url:
            _ = index_re.findall(url)
            if _:
                self.gid, self.sethash = _[0]
        self.failcode = 0
        self.state = TASK_STATE_WAITING
        self.guid = str(uuid.uuid4())[:8]
        self.config = cfgdict
        self.meta = {}
        self.has_ori = False
        self.reload_map = {} # {url:reload_url}
        self.img_q = None
        self.page_q = None
        self.list_q = None
        self._flist_done = set() # store id, don't save, will generate when scan

    def cleanup(self):
        if self.state in (TASK_STATE_FINISHED, TASK_STATE_FAILED):
            self.img_q = None
            self.page_q = None
            self.list_q = None
            self.reload_map = {}
            if 'filelist' in self.meta:
                del self.meta['filelist']
            if 'resampled' in self.meta:
                del self.meta['resampled']

    def set_fail(self, code):
        self.state = TASK_STATE_FAILED
        self.failcode = code
        # cleanup all we cached
        self.meta = {}

    def migrate_exhentai(self):
        _ = re.findall("(?:https*://g\.e\-hentai\.org)(.+)", self.url)
        if not _:
            return False
        self.url = "https://exhentai.org%s" % _[0]
        self.state = TASK_STATE_WAITING if self.state == TASK_STATE_FAILED else self.state
        self.failcode = 0
        return True

    def guess_ori(self):
        # guess if this gallery has resampled files depending on some sample hashes
        # return True if it's ori
        if 'sample_hash' not in self.meta:
            return
        all_keys = map(lambda x:x[:10], self.meta['filelist'].keys())
        for h in self.meta['sample_hash']:
            if h not in all_keys:
                self.has_ori = True
                break
        del self.meta['sample_hash']

    def base_url(self):
        return re.findall("(https*://(?:g\.e\-|ex)hentai\.org)", self.url)[0]

    def get_picpage_url(self, pichash):
        # if file resized, this url not works
        # http://%s.org/s/hash_s/gid-picid'
        return "%s/s/%s/%s-%s" % (
            self.base_url(), pichash[:10], self.gid, self.meta['filelist'][pichash][0]
        )

    def set_reload_url(self, imgurl, reload_url, fname):
        self.reload_map[imgurl] = (reload_url, fname)

    def get_reload_url(self, imgurl):
        if not imgurl:
            return
        return self.reload_map[imgurl][0]

    def scan_downloaded(self, scaled = True):
        fpath = os.path.join(self.config['dir'], util.legalpath(self.meta['title']))
        if not os.path.exists(fpath):
            return
        donefile = False
        if os.path.exists(os.path.join(fpath, ".xehdone")):
            donefile = True
        # can only check un-renamed files
        for h in self.meta['filelist']:
            fid = self.meta['filelist'][h][0]
            fname = os.path.join(fpath, "%03d.jpg" % int(fid)) # id
            if (os.path.exists(fname) and os.stat(fname).st_size > 0) or donefile:
                self._flist_done.add(int(fid))
        self.meta['finished'] = len(self._flist_done)

    def queue_wrapper(self, callback, pichash = None, url = None):
        # if url is not finished, call callback to put into queue
        # type 1: normal file; type 2: resampled url
        if pichash:
            fid = int(self.meta['filelist'][pichash][0])
            if fid not in self._flist_done:
                callback(self.get_picpage_url(pichash))
        elif url:
            fhash, fid = gallery_re.findall(url)[0]
            if fhash not in self.meta['filelist']:
                self.meta['resampled'][fhash] = int(fid)
                self.has_ori = True
            callback(url)

    def save_file(self, imgurl, binary):
        # TODO: Rlock for finished += 1
        self.meta['finished'] += 1

        fpath = os.path.join(self.config['dir'], util.legalpath(self.meta['title']))
        if not os.path.exists(fpath):
            os.mkdir(fpath)
        pageurl, fname = self.reload_map[imgurl]
        _, fid = gallery_re.findall(pageurl)[0]

        fpath = os.path.join(fpath, "%03d.jpg" % int(fid))

        with open(fpath, "wb") as f:
            f.write(binary)

    def rename_ori(self):
        fpath = os.path.join(self.config['dir'], util.legalpath(self.meta['title']))
        cnt = 0
        for h in self.reload_map:
            pageurl, fname = self.reload_map[h]
            _, fid = gallery_re.findall(pageurl)[0]
            fname_ori = os.path.join(fpath, "%03d.jpg" % int(fid)) # id
            fname_to = os.path.join(fpath, util.legalpath(fname))
            if os.path.exists(fname_ori):
                os.rename(fname_ori, fname_to)
                cnt += 1
        if cnt == self.meta['total']:
            with open(os.path.join(fpath, ".xehdone"), "w"):
                pass


    def from_dict(self, j):
        for k in self.__dict__:
            if k not in j:
                continue
            if k.endswith('_q') and j[k]:
                setattr(self, k, Queue())
                [getattr(self, k).put(e, False) for e in j[k]]
            else:
                setattr(self, k, j[k])
        _ = index_re.findall(self.url)
        if _:
            self.gid, self.sethash = _[0]
        return self


    def to_dict(self):
        d = dict({k:v for k, v in self.__dict__.iteritems()
            if not k.endswith('_q') and not k.startswith("_")})
        for k in ['img_q', 'page_q', 'list_q']:
            if getattr(self, k):
                d[k] = [e for e in getattr(self, k).queue]
        return d
