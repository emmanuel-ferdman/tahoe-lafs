"""
Ported to Python 3.
"""
from __future__ import annotations

import os, re, sys, time, json
from typing import Optional

from bs4 import BeautifulSoup

from twisted.trial import unittest
from twisted.internet import defer

from allmydata import uri
from allmydata.storage.mutable import MutableShareFile
from allmydata.storage.immutable import ShareFile
from allmydata.storage.server import si_a2b
from allmydata.immutable import offloaded, upload
from allmydata.immutable.literal import LiteralFileNode
from allmydata.immutable.filenode import ImmutableFileNode
from allmydata.util import idlib, mathutil
from allmydata.util import log, base32
from allmydata.util.encodingutil import quote_output, unicode_to_argv
from allmydata.util.fileutil import abspath_expanduser_unicode
from allmydata.util.consumer import MemoryConsumer, download_to_data
from allmydata.util.deferredutil import async_to_deferred
from allmydata.interfaces import IDirectoryNode, IFileNode, \
     NoSuchChildError, NoSharesError, SDMF_VERSION, MDMF_VERSION
from allmydata.monitor import Monitor
from allmydata.mutable.common import NotWriteableError
from allmydata.mutable import layout as mutable_layout
from allmydata.mutable.publish import MutableData

from foolscap.api import DeadReferenceError, fireEventually
from twisted.python.failure import Failure
from twisted.internet.utils import (
    getProcessOutputAndValue,
)

from .common_web import do_http as do_http_bytes, Error
from .web.common import (
    assert_soup_has_tag_with_attributes
)
from .common_system import SystemTestMixin
from .common_util import run_cli_unicode

def byteschr(x):
    return bytes([x])

class RunBinTahoeMixin:
    def run_bintahoe(self, args, stdin=None, python_options:Optional[list[str]]=None, env=None):
        # test_runner.run_bintahoe has better unicode support but doesn't
        # support env yet and is also synchronous.  If we could get rid of
        # this in favor of that, though, it would probably be an improvement.
        if python_options is None:
            python_options = []
        command = sys.executable
        argv = python_options + ["-b", "-m", "allmydata.scripts.runner"] + args

        if env is None:
            env = os.environ

        d = getProcessOutputAndValue(command, argv, env, stdinBytes=stdin)
        def fix_signal(result):
            # Mirror subprocess.Popen.returncode structure
            (out, err, signal) = result
            return (out, err, -signal)
        d.addErrback(fix_signal)
        return d


def run_cli(*args, **kwargs):
    """
    Run a Tahoe-LAFS CLI utility, but inline.

    Version of run_cli_unicode() that takes any kind of string, and the
    command-line args inline instead of as verb + list.

    Backwards compatible version so we don't have to change all the tests that
    expected this API.
    """
    nodeargs = [a for a in kwargs.pop("nodeargs", [])]
    kwargs["nodeargs"] = nodeargs
    return run_cli_unicode(args[0], [a for a in args[1:]], **kwargs)


def do_http(*args, **kwargs):
    """Wrapper for do_http() that returns Unicode."""
    return do_http_bytes(*args, **kwargs).addCallback(
        lambda b: str(b, "utf-8"))


LARGE_DATA = b"""
This is some data to publish to the remote grid.., which needs to be large
enough to not fit inside a LIT uri.
"""


class CountingDataUploadable(upload.Data):
    bytes_read = 0
    interrupt_after = None
    interrupt_after_d = None

    def read(self, length):
        self.bytes_read += length
        if self.interrupt_after is not None:
            if self.bytes_read > self.interrupt_after:
                self.interrupt_after = None
                self.interrupt_after_d.callback(self)
        return upload.Data.read(self, length)


class SystemTest(SystemTestMixin, RunBinTahoeMixin, unittest.TestCase):
    """Foolscap integration-y tests."""
    FORCE_FOOLSCAP_FOR_STORAGE = True
    timeout = 300

    @property
    def basedir(self):
        return "system/SystemTest/{}-foolscap-{}".format(
            self.id().split(".")[-1], self.FORCE_FOOLSCAP_FOR_STORAGE
        )

    def test_connections(self):
        d = self.set_up_nodes()
        self.extra_node = None
        d.addCallback(lambda res: self.add_extra_node(self.numclients))
        def _check(extra_node):
            self.extra_node = extra_node
            for c in self.clients:
                all_peerids = c.get_storage_broker().get_all_serverids()
                self.failUnlessEqual(len(all_peerids), self.numclients+1)
                sb = c.storage_broker
                permuted_peers = sb.get_servers_for_psi("a")
                self.failUnlessEqual(len(permuted_peers), self.numclients+1)

        d.addCallback(_check)
        def _shutdown_extra_node(res):
            if self.extra_node:
                return self.extra_node.stopService()
            return res
        d.addBoth(_shutdown_extra_node)
        return d
    # test_connections is subsumed by test_upload_and_download, and takes
    # quite a while to run on a slow machine (because of all the TLS
    # connections that must be established). If we ever rework the introducer
    # code to such an extent that we're not sure if it works anymore, we can
    # reinstate this test until it does.
    del test_connections

    def test_upload_and_download_random_key(self):
        return self._test_upload_and_download(convergence=None)

    def test_upload_and_download_convergent(self):
        return self._test_upload_and_download(convergence=b"some convergence string")

    def _test_upload_and_download(self, convergence):
        # we use 4000 bytes of data, which will result in about 400k written
        # to disk among all our simulated nodes
        DATA = b"Some data to upload\n" * 200
        d = self.set_up_nodes()
        def _check_connections(res):
            for c in self.clients:
                c.encoding_params['happy'] = 5
                all_peerids = c.get_storage_broker().get_all_serverids()
                self.failUnlessEqual(len(all_peerids), self.numclients)
                sb = c.storage_broker
                permuted_peers = sb.get_servers_for_psi(b"a")
                self.failUnlessEqual(len(permuted_peers), self.numclients)
        d.addCallback(_check_connections)

        def _do_upload(res):
            log.msg("UPLOADING")
            u = self.clients[0].getServiceNamed("uploader")
            self.uploader = u
            # we crank the max segsize down to 1024b for the duration of this
            # test, so we can exercise multiple segments. It is important
            # that this is not a multiple of the segment size, so that the
            # tail segment is not the same length as the others. This actualy
            # gets rounded up to 1025 to be a multiple of the number of
            # required shares (since we use 25 out of 100 FEC).
            up = upload.Data(DATA, convergence=convergence)
            up.max_segment_size = 1024
            d1 = u.upload(up)
            return d1
        d.addCallback(_do_upload)
        def _upload_done(results):
            theuri = results.get_uri()
            log.msg("upload finished: uri is %r" % (theuri,))
            self.uri = theuri
            assert isinstance(self.uri, bytes), self.uri
            self.cap = uri.from_string(self.uri)
            self.n = self.clients[1].create_node_from_uri(self.uri)
        d.addCallback(_upload_done)

        def _upload_again(res):
            # Upload again. If using convergent encryption then this ought to be
            # short-circuited, however with the way we currently generate URIs
            # (i.e. because they include the roothash), we have to do all of the
            # encoding work, and only get to save on the upload part.
            log.msg("UPLOADING AGAIN")
            up = upload.Data(DATA, convergence=convergence)
            up.max_segment_size = 1024
            return self.uploader.upload(up)
        d.addCallback(_upload_again)

        def _download_to_data(res):
            log.msg("DOWNLOADING")
            return download_to_data(self.n)
        d.addCallback(_download_to_data)
        def _download_to_data_done(data):
            log.msg("download finished")
            self.failUnlessEqual(data, DATA)
        d.addCallback(_download_to_data_done)

        def _test_read(res):
            n = self.clients[1].create_node_from_uri(self.uri)
            d = download_to_data(n)
            def _read_done(data):
                self.failUnlessEqual(data, DATA)
            d.addCallback(_read_done)
            d.addCallback(lambda ign:
                          n.read(MemoryConsumer(), offset=1, size=4))
            def _read_portion_done(mc):
                self.failUnlessEqual(b"".join(mc.chunks), DATA[1:1+4])
            d.addCallback(_read_portion_done)
            d.addCallback(lambda ign:
                          n.read(MemoryConsumer(), offset=2, size=None))
            def _read_tail_done(mc):
                self.failUnlessEqual(b"".join(mc.chunks), DATA[2:])
            d.addCallback(_read_tail_done)
            d.addCallback(lambda ign:
                          n.read(MemoryConsumer(), size=len(DATA)+1000))
            def _read_too_much(mc):
                self.failUnlessEqual(b"".join(mc.chunks), DATA)
            d.addCallback(_read_too_much)

            return d
        d.addCallback(_test_read)

        def _test_bad_read(res):
            bad_u = uri.from_string_filenode(self.uri)
            bad_u.key = self.flip_bit(bad_u.key)
            bad_n = self.clients[1].create_node_from_uri(bad_u.to_string())
            # this should cause an error during download

            d = self.shouldFail2(NoSharesError, "'download bad node'",
                                 None,
                                 bad_n.read, MemoryConsumer(), offset=2)
            return d
        d.addCallback(_test_bad_read)

        def _download_nonexistent_uri(res):
            baduri = self.mangle_uri(self.uri)
            badnode = self.clients[1].create_node_from_uri(baduri)
            log.msg("about to download non-existent URI", level=log.UNUSUAL,
                    facility="tahoe.tests")
            d1 = download_to_data(badnode)
            def _baduri_should_fail(res):
                log.msg("finished downloading non-existent URI",
                        level=log.UNUSUAL, facility="tahoe.tests")
                self.failUnless(isinstance(res, Failure))
                self.failUnless(res.check(NoSharesError),
                                "expected NoSharesError, got %s" % res)
            d1.addBoth(_baduri_should_fail)
            return d1
        d.addCallback(_download_nonexistent_uri)

        # add a new node, which doesn't accept shares, and only uses the
        # helper for upload.
        d.addCallback(lambda res: self.add_extra_node(self.numclients,
                                                      self.helper_furl,
                                                      add_to_sparent=True))
        def _added(extra_node):
            self.extra_node = extra_node
            self.extra_node.encoding_params['happy'] = 5
        d.addCallback(_added)

        def _has_helper():
            uploader = self.extra_node.getServiceNamed("uploader")
            furl, connected = uploader.get_helper_info()
            return connected
        d.addCallback(lambda ign: self.poll(_has_helper))

        HELPER_DATA = b"Data that needs help to upload" * 1000
        def _upload_with_helper(res):
            u = upload.Data(HELPER_DATA, convergence=convergence)
            d = self.extra_node.upload(u)
            def _uploaded(results):
                n = self.clients[1].create_node_from_uri(results.get_uri())
                return download_to_data(n)
            d.addCallback(_uploaded)
            def _check(newdata):
                self.failUnlessEqual(newdata, HELPER_DATA)
            d.addCallback(_check)
            return d
        d.addCallback(_upload_with_helper)

        def _upload_duplicate_with_helper(res):
            u = upload.Data(HELPER_DATA, convergence=convergence)
            u.debug_stash_RemoteEncryptedUploadable = True
            d = self.extra_node.upload(u)
            def _uploaded(results):
                n = self.clients[1].create_node_from_uri(results.get_uri())
                return download_to_data(n)
            d.addCallback(_uploaded)
            def _check(newdata):
                self.failUnlessEqual(newdata, HELPER_DATA)
                self.failIf(hasattr(u, "debug_RemoteEncryptedUploadable"),
                            "uploadable started uploading, should have been avoided")
            d.addCallback(_check)
            return d
        if convergence is not None:
            d.addCallback(_upload_duplicate_with_helper)

        d.addCallback(fireEventually)

        def _upload_resumable(res):
            DATA = b"Data that needs help to upload and gets interrupted" * 1000
            u1 = CountingDataUploadable(DATA, convergence=convergence)
            u2 = CountingDataUploadable(DATA, convergence=convergence)

            # we interrupt the connection after about 5kB by shutting down
            # the helper, then restarting it.
            u1.interrupt_after = 5000
            u1.interrupt_after_d = defer.Deferred()
            bounced_d = defer.Deferred()
            def _do_bounce(res):
                d = self.bounce_client(0)
                d.addBoth(bounced_d.callback)
            u1.interrupt_after_d.addCallback(_do_bounce)

            # sneak into the helper and reduce its chunk size, so that our
            # debug_interrupt will sever the connection on about the fifth
            # chunk fetched. This makes sure that we've started to write the
            # new shares before we abandon them, which exercises the
            # abort/delete-partial-share code. TODO: find a cleaner way to do
            # this. I know that this will affect later uses of the helper in
            # this same test run, but I'm not currently worried about it.
            offloaded.CHKCiphertextFetcher.CHUNK_SIZE = 1000

            upload_d = self.extra_node.upload(u1)
            # The upload will start, and bounce_client() will be called after
            # about 5kB. bounced_d will fire after bounce_client() finishes
            # shutting down and restarting the node.
            d = bounced_d
            def _bounced(ign):
                # By this point, the upload should have failed because of the
                # interruption. upload_d will fire in a moment
                def _should_not_finish(res):
                    self.fail("interrupted upload should have failed, not"
                              " finished with result %s" % (res,))
                def _interrupted(f):
                    f.trap(DeadReferenceError)
                    # make sure we actually interrupted it before finishing
                    # the file
                    self.failUnless(u1.bytes_read < len(DATA),
                                    "read %d out of %d total" %
                                    (u1.bytes_read, len(DATA)))
                upload_d.addCallbacks(_should_not_finish, _interrupted)
                return upload_d
            d.addCallback(_bounced)

            def _disconnected(res):
                # check to make sure the storage servers aren't still hanging
                # on to the partial share: their incoming/ directories should
                # now be empty.
                log.msg("disconnected", level=log.NOISY,
                        facility="tahoe.test.test_system")
                for i in range(self.numclients):
                    incdir = os.path.join(self.getdir("client%d" % i),
                                          "storage", "shares", "incoming")
                    self.failIf(os.path.exists(incdir) and os.listdir(incdir))
            d.addCallback(_disconnected)

            d.addCallback(lambda res:
                          log.msg("wait_for_helper", level=log.NOISY,
                                  facility="tahoe.test.test_system"))
            # then we need to wait for the extra node to reestablish its
            # connection to the helper.
            d.addCallback(lambda ign: self.poll(_has_helper))

            d.addCallback(lambda res:
                          log.msg("uploading again", level=log.NOISY,
                                  facility="tahoe.test.test_system"))
            d.addCallback(lambda res: self.extra_node.upload(u2))

            def _uploaded(results):
                cap = results.get_uri()
                log.msg("Second upload complete", level=log.NOISY,
                        facility="tahoe.test.test_system")

                # this is really bytes received rather than sent, but it's
                # convenient and basically measures the same thing
                bytes_sent = results.get_ciphertext_fetched()
                self.failUnless(isinstance(bytes_sent, int), bytes_sent)

                # We currently don't support resumption of upload if the data is
                # encrypted with a random key.  (Because that would require us
                # to store the key locally and re-use it on the next upload of
                # this file, which isn't a bad thing to do, but we currently
                # don't do it.)
                if convergence is not None:
                    # Make sure we did not have to read the whole file the
                    # second time around .
                    self.failUnless(bytes_sent < len(DATA),
                                    "resumption didn't save us any work:"
                                    " read %r bytes out of %r total" %
                                    (bytes_sent, len(DATA)))
                else:
                    # Make sure we did have to read the whole file the second
                    # time around -- because the one that we partially uploaded
                    # earlier was encrypted with a different random key.
                    self.failIf(bytes_sent < len(DATA),
                                "resumption saved us some work even though we were using random keys:"
                                " read %r bytes out of %r total" %
                                (bytes_sent, len(DATA)))
                n = self.clients[1].create_node_from_uri(cap)
                return download_to_data(n)
            d.addCallback(_uploaded)

            def _check(newdata):
                self.failUnlessEqual(newdata, DATA)
                # If using convergent encryption, then also check that the
                # helper has removed the temp file from its directories.
                if convergence is not None:
                    basedir = os.path.join(self.getdir("client0"), "helper")
                    files = os.listdir(os.path.join(basedir, "CHK_encoding"))
                    self.failUnlessEqual(files, [])
                    files = os.listdir(os.path.join(basedir, "CHK_incoming"))
                    self.failUnlessEqual(files, [])
            d.addCallback(_check)
            return d
        d.addCallback(_upload_resumable)

        def _grab_stats(ignored):
            stats = self.clients[0].stats_provider.get_stats()
            s = stats["stats"]
            self.failUnlessEqual(s["storage_server.accepting_immutable_shares"], 1)
            c = stats["counters"]
            self.failUnless("storage_server.allocate" in c)
        d.addCallback(_grab_stats)

        return d

    def _find_all_shares(self, basedir):
        shares = []
        for (dirpath, dirnames, filenames) in os.walk(basedir):
            if "storage" not in dirpath:
                continue
            if not filenames:
                continue
            pieces = dirpath.split(os.sep)
            if (len(pieces) >= 5
                and pieces[-4] == "storage"
                and pieces[-3] == "shares"):
                # we're sitting in .../storage/shares/$START/$SINDEX , and there
                # are sharefiles here
                assert pieces[-5].startswith("client")
                client_num = int(pieces[-5][-1])
                storage_index_s = pieces[-1]
                storage_index = si_a2b(storage_index_s.encode("ascii"))
                for sharename in filenames:
                    shnum = int(sharename)
                    filename = os.path.join(dirpath, sharename)
                    data = (client_num, storage_index, filename, shnum)
                    shares.append(data)
        if not shares:
            self.fail("unable to find any share files in %s" % basedir)
        return shares

    def _corrupt_mutable_share(self, filename, which):
        msf = MutableShareFile(filename)
        # Read more than share length:
        datav = msf.readv([ (0, 10_000_000) ])
        final_share = datav[0]
        assert len(final_share) < 10_000_000 # ought to be truncated
        pieces = mutable_layout.unpack_share(final_share)
        (seqnum, root_hash, IV, k, N, segsize, datalen,
         verification_key, signature, share_hash_chain, block_hash_tree,
         share_data, enc_privkey) = pieces

        if which == "seqnum":
            seqnum = seqnum + 15
        elif which == "R":
            root_hash = self.flip_bit(root_hash)
        elif which == "IV":
            IV = self.flip_bit(IV)
        elif which == "segsize":
            segsize = segsize + 15
        elif which == "pubkey":
            verification_key = self.flip_bit(verification_key)
        elif which == "signature":
            signature = self.flip_bit(signature)
        elif which == "share_hash_chain":
            nodenum = list(share_hash_chain.keys())[0]
            share_hash_chain[nodenum] = self.flip_bit(share_hash_chain[nodenum])
        elif which == "block_hash_tree":
            block_hash_tree[-1] = self.flip_bit(block_hash_tree[-1])
        elif which == "share_data":
            share_data = self.flip_bit(share_data)
        elif which == "encprivkey":
            enc_privkey = self.flip_bit(enc_privkey)

        prefix = mutable_layout.pack_prefix(seqnum, root_hash, IV, k, N,
                                            segsize, datalen)
        final_share = mutable_layout.pack_share(prefix,
                                                verification_key,
                                                signature,
                                                share_hash_chain,
                                                block_hash_tree,
                                                share_data,
                                                enc_privkey)
        msf.writev( [(0, final_share)], None)


    def test_mutable_sdmf(self):
        """SDMF mutables can be uploaded, downloaded, and many other things."""
        return self._test_mutable(SDMF_VERSION)

    def test_mutable_mdmf(self):
        """MDMF mutables can be uploaded, downloaded, and many other things."""
        return self._test_mutable(MDMF_VERSION)

    def _test_mutable(self, mutable_version):
        DATA = b"initial contents go here."  # 25 bytes % 3 != 0
        DATA_uploadable = MutableData(DATA)
        NEWDATA = b"new contents yay"
        NEWDATA_uploadable = MutableData(NEWDATA)
        NEWERDATA = b"this is getting old" * 1_000_000
        NEWERDATA_uploadable = MutableData(NEWERDATA)

        d = self.set_up_nodes()

        def _create_mutable(res):
            c = self.clients[0]
            log.msg("starting create_mutable_file")
            d1 = c.create_mutable_file(DATA_uploadable, mutable_version)
            def _done(res):
                log.msg("DONE: %s" % (res,))
                self._mutable_node_1 = res
            d1.addCallback(_done)
            return d1
        d.addCallback(_create_mutable)

        @defer.inlineCallbacks
        def _test_debug(res):
            # find a share. It is important to run this while there is only
            # one slot in the grid.
            shares = self._find_all_shares(self.basedir)
            (client_num, storage_index, filename, shnum) = shares[0]
            log.msg("test_system.SystemTest.test_mutable._test_debug using %s"
                    % filename)
            log.msg(" for clients[%d]" % client_num)

            rc,output,err = yield run_cli("debug", "dump-share", "--offsets",
                                          filename)
            self.failUnlessEqual(rc, 0)
            try:
                share_type = 'SDMF' if mutable_version == SDMF_VERSION else 'MDMF'
                self.failUnless("Mutable slot found:\n" in output)
                self.assertIn(f"share_type: {share_type}\n", output)
                peerid = idlib.nodeid_b2a(self.clients[client_num].nodeid)
                self.failUnless(" WE for nodeid: %s\n" % peerid in output)
                self.failUnless(" num_extra_leases: 0\n" in output)
                self.failUnless("  secrets are for nodeid: %s\n" % peerid
                                in output)
                self.failUnless(f" {share_type} contents:\n" in output)
                self.failUnless("  seqnum: 1\n" in output)
                self.failUnless("  required_shares: 3\n" in output)
                self.failUnless("  total_shares: 10\n" in output)
                if mutable_version == SDMF_VERSION:
                    self.failUnless("  segsize: 27\n" in output, (output, filename))
                self.failUnless("  datalen: 25\n" in output)
                # the exact share_hash_chain nodes depends upon the sharenum,
                # and is more of a hassle to compute than I want to deal with
                # now
                self.failUnless("  share_hash_chain: " in output)
                self.failUnless("  block_hash_tree: 1 nodes\n" in output)
                if mutable_version == SDMF_VERSION:
                    expected = ("  verify-cap: URI:SSK-Verifier:%s:" %
                                str(base32.b2a(storage_index), "ascii"))
                else:
                    expected = ("  verify-cap: URI:MDMF-Verifier:%s" %
                                str(base32.b2a(storage_index), "ascii"))
                self.assertIn(expected, output)
            except unittest.FailTest:
                print()
                print("dump-share output was:")
                print(output)
                raise
        d.addCallback(_test_debug)

        # test retrieval

        # first, let's see if we can use the existing node to retrieve the
        # contents. This allows it to use the cached pubkey and maybe the
        # latest-known sharemap.

        d.addCallback(lambda res: self._mutable_node_1.download_best_version())
        def _check_download_1(res):
            self.failUnlessEqual(res, DATA)
            # now we see if we can retrieve the data from a new node,
            # constructed using the URI of the original one. We do this test
            # on the same client that uploaded the data.
            uri = self._mutable_node_1.get_uri()
            log.msg("starting retrieve1")
            newnode = self.clients[0].create_node_from_uri(uri)
            newnode_2 = self.clients[0].create_node_from_uri(uri)
            self.failUnlessIdentical(newnode, newnode_2)
            return newnode.download_best_version()
        d.addCallback(_check_download_1)

        def _check_download_2(res):
            self.failUnlessEqual(res, DATA)
            # same thing, but with a different client
            uri = self._mutable_node_1.get_uri()
            newnode = self.clients[1].create_node_from_uri(uri)
            log.msg("starting retrieve2")
            d1 = newnode.download_best_version()
            d1.addCallback(lambda res: (res, newnode))
            return d1
        d.addCallback(_check_download_2)

        def _check_download_3(res_and_newnode):
            (res, newnode) = res_and_newnode
            self.failUnlessEqual(res, DATA)
            # replace the data
            log.msg("starting replace1")
            d1 = newnode.overwrite(NEWDATA_uploadable)
            d1.addCallback(lambda res: newnode.download_best_version())
            return d1
        d.addCallback(_check_download_3)

        def _check_download_4(res):
            self.failUnlessEqual(res, NEWDATA)
            # now create an even newer node and replace the data on it. This
            # new node has never been used for download before.
            uri = self._mutable_node_1.get_uri()
            newnode1 = self.clients[2].create_node_from_uri(uri)
            newnode2 = self.clients[3].create_node_from_uri(uri)
            self._newnode3 = self.clients[3].create_node_from_uri(uri)
            log.msg("starting replace2")
            d1 = newnode1.overwrite(NEWERDATA_uploadable)
            d1.addCallback(lambda res: newnode2.download_best_version())
            return d1
        d.addCallback(_check_download_4)

        def _check_download_5(res):
            log.msg("finished replace2")
            self.failUnlessEqual(res, NEWERDATA)
        d.addCallback(_check_download_5)

        # The previous checks upload a complete replacement. This uses a
        # different API that is supposed to do a partial write at an offset.
        @async_to_deferred
        async def _check_write_at_offset(newnode):
            log.msg("writing at offset")
            start = b"abcdef"
            expected = b"abXYef"
            uri = self._mutable_node_1.get_uri()
            newnode = self.clients[0].create_node_from_uri(uri)
            await newnode.overwrite(MutableData(start))
            version = await newnode.get_mutable_version()
            await version.update(MutableData(b"XY"), 2)
            result = await newnode.download_best_version()
            self.assertEqual(result, expected)
            # Revert to previous version
            await newnode.overwrite(MutableData(NEWERDATA))
        d.addCallback(_check_write_at_offset)

        def _corrupt_shares(_res):
            # run around and flip bits in all but k of the shares, to test
            # the hash checks
            shares = self._find_all_shares(self.basedir)
            ## sort by share number
            #shares.sort( lambda a,b: cmp(a[3], b[3]) )
            where = dict([ (shnum, filename)
                           for (client_num, storage_index, filename, shnum)
                           in shares ])
            assert len(where) == 10 # this test is designed for 3-of-10
            for shnum, filename in list(where.items()):
                # shares 7,8,9 are left alone. read will check
                # (share_hash_chain, block_hash_tree, share_data). New
                # seqnum+R pairs will trigger a check of (seqnum, R, IV,
                # segsize, signature).
                if shnum == 0:
                    # read: this will trigger "pubkey doesn't match
                    # fingerprint".
                    self._corrupt_mutable_share(filename, "pubkey")
                    self._corrupt_mutable_share(filename, "encprivkey")
                elif shnum == 1:
                    # triggers "signature is invalid"
                    self._corrupt_mutable_share(filename, "seqnum")
                elif shnum == 2:
                    # triggers "signature is invalid"
                    self._corrupt_mutable_share(filename, "R")
                elif shnum == 3:
                    # triggers "signature is invalid"
                    self._corrupt_mutable_share(filename, "segsize")
                elif shnum == 4:
                    self._corrupt_mutable_share(filename, "share_hash_chain")
                elif shnum == 5:
                    self._corrupt_mutable_share(filename, "block_hash_tree")
                elif shnum == 6:
                    self._corrupt_mutable_share(filename, "share_data")
                # other things to correct: IV, signature
                # 7,8,9 are left alone

                # note that initial_query_count=5 means that we'll hit the
                # first 5 servers in effectively random order (based upon
                # response time), so we won't necessarily ever get a "pubkey
                # doesn't match fingerprint" error (if we hit shnum>=1 before
                # shnum=0, we pull the pubkey from there). To get repeatable
                # specific failures, we need to set initial_query_count=1,
                # but of course that will change the sequencing behavior of
                # the retrieval process. TODO: find a reasonable way to make
                # this a parameter, probably when we expand this test to test
                # for one failure mode at a time.

                # when we retrieve this, we should get three signature
                # failures (where we've mangled seqnum, R, and segsize). The
                # pubkey mangling

        if mutable_version == SDMF_VERSION:
            # TODO Corrupting shares in test_systm doesn't work for MDMF right now
            d.addCallback(_corrupt_shares)

        d.addCallback(lambda res: self._newnode3.download_best_version())
        d.addCallback(_check_download_5)

        def _check_empty_file(res):
            # make sure we can create empty files, this usually screws up the
            # segsize math
            d1 = self.clients[2].create_mutable_file(MutableData(b""), mutable_version)
            d1.addCallback(lambda newnode: newnode.download_best_version())
            d1.addCallback(lambda res: self.failUnlessEqual(b"", res))
            return d1
        d.addCallback(_check_empty_file)

        d.addCallback(lambda res: self.clients[0].create_dirnode())
        def _created_dirnode(dnode):
            log.msg("_created_dirnode(%s)" % (dnode,))
            d1 = dnode.list()
            d1.addCallback(lambda children: self.failUnlessEqual(children, {}))
            d1.addCallback(lambda res: dnode.has_child(u"edgar"))
            d1.addCallback(lambda answer: self.failUnlessEqual(answer, False))
            d1.addCallback(lambda res: dnode.set_node(u"see recursive", dnode))
            d1.addCallback(lambda res: dnode.has_child(u"see recursive"))
            d1.addCallback(lambda answer: self.failUnlessEqual(answer, True))
            d1.addCallback(lambda res: dnode.build_manifest().when_done())
            d1.addCallback(lambda res:
                           self.failUnlessEqual(len(res["manifest"]), 1))
            return d1
        d.addCallback(_created_dirnode)

        return d

    def flip_bit(self, good):
        return good[:-1] + byteschr(ord(good[-1:]) ^ 0x01)

    def mangle_uri(self, gooduri):
        # change the key, which changes the storage index, which means we'll
        # be asking about the wrong file, so nobody will have any shares
        u = uri.from_string(gooduri)
        u2 = uri.CHKFileURI(key=self.flip_bit(u.key),
                            uri_extension_hash=u.uri_extension_hash,
                            needed_shares=u.needed_shares,
                            total_shares=u.total_shares,
                            size=u.size)
        return u2.to_string()

    # TODO: add a test which mangles the uri_extension_hash instead, and
    # should fail due to not being able to get a valid uri_extension block.
    # Also a test which sneakily mangles the uri_extension block to change
    # some of the validation data, so it will fail in the post-download phase
    # when the file's crypttext integrity check fails. Do the same thing for
    # the key, which should cause the download to fail the post-download
    # plaintext_hash check.

    def test_filesystem(self):
        self.data = LARGE_DATA
        d = self.set_up_nodes(2)
        def _new_happy_semantics(ign):
            for c in self.clients:
                c.encoding_params['happy'] = 1
        d.addCallback(_new_happy_semantics)
        d.addCallback(self.log, "starting publish")
        d.addCallback(self._do_publish1)
        d.addCallback(self._test_runner)
        d.addCallback(self._do_publish2)
        # at this point, we have the following filesystem (where "R" denotes
        # self._root_directory_uri):
        # R
        # R/subdir1
        # R/subdir1/mydata567
        # R/subdir1/subdir2/
        # R/subdir1/subdir2/mydata992

        d.addCallback(lambda res: self.bounce_client(0))
        d.addCallback(self.log, "bounced client0")

        d.addCallback(self._check_publish1)
        d.addCallback(self.log, "did _check_publish1")
        d.addCallback(self._check_publish2)
        d.addCallback(self.log, "did _check_publish2")
        d.addCallback(self._do_publish_private)
        d.addCallback(self.log, "did _do_publish_private")
        # now we also have (where "P" denotes a new dir):
        #  P/personal/sekrit data
        #  P/s2-rw -> /subdir1/subdir2/
        #  P/s2-ro -> /subdir1/subdir2/ (read-only)
        d.addCallback(self._check_publish_private)
        d.addCallback(self.log, "did _check_publish_private")
        d.addCallback(self._test_web)
        d.addCallback(self._test_cli)
        # P now has four top-level children:
        # P/personal/sekrit data
        # P/s2-ro/
        # P/s2-rw/
        # P/test_put/  (empty)
        d.addCallback(self._test_checker)
        return d

    def _do_publish1(self, res):
        ut = upload.Data(self.data, convergence=None)
        c0 = self.clients[0]
        d = c0.create_dirnode()
        def _made_root(new_dirnode):
            self._root_directory_uri = new_dirnode.get_uri()
            return c0.create_node_from_uri(self._root_directory_uri)
        d.addCallback(_made_root)
        d.addCallback(lambda root: root.create_subdirectory(u"subdir1"))
        def _made_subdir1(subdir1_node):
            self._subdir1_node = subdir1_node
            d1 = subdir1_node.add_file(u"mydata567", ut)
            d1.addCallback(self.log, "publish finished")
            def _stash_uri(filenode):
                self.uri = filenode.get_uri()
                assert isinstance(self.uri, bytes), (self.uri, filenode)
            d1.addCallback(_stash_uri)
            return d1
        d.addCallback(_made_subdir1)
        return d

    def _do_publish2(self, res):
        ut = upload.Data(self.data, convergence=None)
        d = self._subdir1_node.create_subdirectory(u"subdir2")
        d.addCallback(lambda subdir2: subdir2.add_file(u"mydata992", ut))
        return d

    def log(self, res, *args, **kwargs):
        # print("MSG: %s  RES: %s" % (msg, args))
        log.msg(*args, **kwargs)
        return res

    def _do_publish_private(self, res):
        self.smalldata = b"sssh, very secret stuff"
        ut = upload.Data(self.smalldata, convergence=None)
        d = self.clients[0].create_dirnode()
        d.addCallback(self.log, "GOT private directory")
        def _got_new_dir(privnode):
            rootnode = self.clients[0].create_node_from_uri(self._root_directory_uri)
            d1 = privnode.create_subdirectory(u"personal")
            d1.addCallback(self.log, "made P/personal")
            d1.addCallback(lambda node: node.add_file(u"sekrit data", ut))
            d1.addCallback(self.log, "made P/personal/sekrit data")
            d1.addCallback(lambda res: rootnode.get_child_at_path([u"subdir1", u"subdir2"]))
            def _got_s2(s2node):
                d2 = privnode.set_uri(u"s2-rw", s2node.get_uri(),
                                      s2node.get_readonly_uri())
                d2.addCallback(lambda node:
                               privnode.set_uri(u"s2-ro",
                                                s2node.get_readonly_uri(),
                                                s2node.get_readonly_uri()))
                return d2
            d1.addCallback(_got_s2)
            d1.addCallback(lambda res: privnode)
            return d1
        d.addCallback(_got_new_dir)
        return d

    def _check_publish1(self, res):
        # this one uses the iterative API
        c1 = self.clients[1]
        d = defer.succeed(c1.create_node_from_uri(self._root_directory_uri))
        d.addCallback(self.log, "check_publish1 got /")
        d.addCallback(lambda root: root.get(u"subdir1"))
        d.addCallback(lambda subdir1: subdir1.get(u"mydata567"))
        d.addCallback(lambda filenode: download_to_data(filenode))
        d.addCallback(self.log, "get finished")
        def _get_done(data):
            self.failUnlessEqual(data, self.data)
        d.addCallback(_get_done)
        return d

    def _check_publish2(self, res):
        # this one uses the path-based API
        rootnode = self.clients[1].create_node_from_uri(self._root_directory_uri)
        d = rootnode.get_child_at_path(u"subdir1")
        d.addCallback(lambda dirnode:
                      self.failUnless(IDirectoryNode.providedBy(dirnode)))
        d.addCallback(lambda res: rootnode.get_child_at_path(u"subdir1/mydata567"))
        d.addCallback(lambda filenode: download_to_data(filenode))
        d.addCallback(lambda data: self.failUnlessEqual(data, self.data))

        d.addCallback(lambda res: rootnode.get_child_at_path(u"subdir1/mydata567"))
        def _got_filenode(filenode):
            fnode = self.clients[1].create_node_from_uri(filenode.get_uri())
            assert fnode == filenode
        d.addCallback(_got_filenode)
        return d

    def _check_publish_private(self, resnode):
        # this one uses the path-based API
        self._private_node = resnode

        d = self._private_node.get_child_at_path(u"personal")
        def _got_personal(personal):
            self._personal_node = personal
            return personal
        d.addCallback(_got_personal)

        d.addCallback(lambda dirnode:
                      self.failUnless(IDirectoryNode.providedBy(dirnode), dirnode))
        def get_path(path):
            return self._private_node.get_child_at_path(path)

        d.addCallback(lambda res: get_path(u"personal/sekrit data"))
        d.addCallback(lambda filenode: download_to_data(filenode))
        d.addCallback(lambda data: self.failUnlessEqual(data, self.smalldata))
        d.addCallback(lambda res: get_path(u"s2-rw"))
        d.addCallback(lambda dirnode: self.failUnless(dirnode.is_mutable()))
        d.addCallback(lambda res: get_path(u"s2-ro"))
        def _got_s2ro(dirnode):
            self.failUnless(dirnode.is_mutable(), dirnode)
            self.failUnless(dirnode.is_readonly(), dirnode)
            d1 = defer.succeed(None)
            d1.addCallback(lambda res: dirnode.list())
            d1.addCallback(self.log, "dirnode.list")

            d1.addCallback(lambda res: self.shouldFail2(NotWriteableError, "mkdir(nope)", None, dirnode.create_subdirectory, u"nope"))

            d1.addCallback(self.log, "doing add_file(ro)")
            ut = upload.Data(b"I will disappear, unrecorded and unobserved. The tragedy of my demise is made more poignant by its silence, but this beauty is not for you to ever know.", convergence=b"99i-p1x4-xd4-18yc-ywt-87uu-msu-zo -- completely and totally unguessable string (unless you read this)")
            d1.addCallback(lambda res: self.shouldFail2(NotWriteableError, "add_file(nope)", None, dirnode.add_file, u"hope", ut))

            d1.addCallback(self.log, "doing get(ro)")
            d1.addCallback(lambda res: dirnode.get(u"mydata992"))
            d1.addCallback(lambda filenode:
                           self.failUnless(IFileNode.providedBy(filenode)))

            d1.addCallback(self.log, "doing delete(ro)")
            d1.addCallback(lambda res: self.shouldFail2(NotWriteableError, "delete(nope)", None, dirnode.delete, u"mydata992"))

            d1.addCallback(lambda res: self.shouldFail2(NotWriteableError, "set_uri(nope)", None, dirnode.set_uri, u"hopeless", self.uri, self.uri))

            d1.addCallback(lambda res: self.shouldFail2(NoSuchChildError, "get(missing)", "missing", dirnode.get, u"missing"))

            personal = self._personal_node
            d1.addCallback(lambda res: self.shouldFail2(NotWriteableError, "mv from readonly", None, dirnode.move_child_to, u"mydata992", personal, u"nope"))

            d1.addCallback(self.log, "doing move_child_to(ro)2")
            d1.addCallback(lambda res: self.shouldFail2(NotWriteableError, "mv to readonly", None, personal.move_child_to, u"sekrit data", dirnode, u"nope"))

            d1.addCallback(self.log, "finished with _got_s2ro")
            return d1
        d.addCallback(_got_s2ro)
        def _got_home(dummy):
            home = self._private_node
            personal = self._personal_node
            d1 = defer.succeed(None)
            d1.addCallback(self.log, "mv 'P/personal/sekrit data' to P/sekrit")
            d1.addCallback(lambda res:
                           personal.move_child_to(u"sekrit data",home,u"sekrit"))

            d1.addCallback(self.log, "mv P/sekrit 'P/sekrit data'")
            d1.addCallback(lambda res:
                           home.move_child_to(u"sekrit", home, u"sekrit data"))

            d1.addCallback(self.log, "mv 'P/sekret data' P/personal/")
            d1.addCallback(lambda res:
                           home.move_child_to(u"sekrit data", personal))

            d1.addCallback(lambda res: home.build_manifest().when_done())
            d1.addCallback(self.log, "manifest")
            #  five items:
            # P/
            # P/personal/
            # P/personal/sekrit data
            # P/s2-rw  (same as P/s2-ro)
            # P/s2-rw/mydata992 (same as P/s2-rw/mydata992)
            d1.addCallback(lambda res:
                           self.failUnlessEqual(len(res["manifest"]), 5))
            d1.addCallback(lambda res: home.start_deep_stats().when_done())
            def _check_stats(stats):
                expected = {"count-immutable-files": 1,
                            "count-mutable-files": 0,
                            "count-literal-files": 1,
                            "count-files": 2,
                            "count-directories": 3,
                            "size-immutable-files": 112,
                            "size-literal-files": 23,
                            #"size-directories": 616, # varies
                            #"largest-directory": 616,
                            "largest-directory-children": 3,
                            "largest-immutable-file": 112,
                            }
                for k,v in list(expected.items()):
                    self.failUnlessEqual(stats[k], v,
                                         "stats[%s] was %s, not %s" %
                                         (k, stats[k], v))
                self.failUnless(stats["size-directories"] > 1300,
                                stats["size-directories"])
                self.failUnless(stats["largest-directory"] > 800,
                                stats["largest-directory"])
                self.failUnlessEqual(stats["size-files-histogram"],
                                     [ (11, 31, 1), (101, 316, 1) ])
            d1.addCallback(_check_stats)
            return d1
        d.addCallback(_got_home)
        return d

    def shouldFail(self, res, expected_failure, which, substring=None):
        if isinstance(res, Failure):
            res.trap(expected_failure)
            if substring:
                self.failUnless(substring in str(res),
                                "substring '%s' not in '%s'"
                                % (substring, str(res)))
        else:
            self.fail("%s was supposed to raise %s, not get '%s'" %
                      (which, expected_failure, res))

    def shouldFail2(self, expected_failure, which, substring, callable, *args, **kwargs):
        assert substring is None or isinstance(substring, str)
        d = defer.maybeDeferred(callable, *args, **kwargs)
        def done(res):
            if isinstance(res, Failure):
                res.trap(expected_failure)
                if substring:
                    self.failUnless(substring in str(res),
                                    "substring '%s' not in '%s'"
                                    % (substring, str(res)))
            else:
                self.fail("%s was supposed to raise %s, not get '%s'" %
                          (which, expected_failure, res))
        d.addBoth(done)
        return d

    def PUT(self, urlpath, data):
        return do_http("put", self.webish_url + urlpath, data=data)

    def GET(self, urlpath):
        return do_http("get", self.webish_url + urlpath)

    def POST(self, urlpath, use_helper=False, **fields):
        sepbase = b"boogabooga"
        sep = b"--" + sepbase
        form = []
        form.append(sep)
        form.append(b'Content-Disposition: form-data; name="_charset"')
        form.append(b'')
        form.append(b'UTF-8')
        form.append(sep)
        for name, value in fields.items():
            if isinstance(value, tuple):
                filename, value = value
                form.append(b'Content-Disposition: form-data; name="%s"; '
                            b'filename="%s"' % (name.encode("utf-8"), filename.encode("utf-8")))
            else:
                form.append(b'Content-Disposition: form-data; name="%s"' % name.encode("utf-8"))
            form.append(b'')
            form.append(b"%s" % (value,))
            form.append(sep)
        form[-1] += b"--"
        body = b""
        headers = {}
        if fields:
            body = b"\r\n".join(form) + b"\r\n"
            headers["content-type"] = "multipart/form-data; boundary=%s" % str(sepbase, "ascii")
        return self.POST2(urlpath, body, headers, use_helper)

    def POST2(self, urlpath, body=b"", headers=None, use_helper=False):
        if headers is None:
            headers = {}
        if use_helper:
            url = self.helper_webish_url + urlpath
        else:
            url = self.webish_url + urlpath
        return do_http("post", url, data=body, headers=headers)

    def _test_web(self, res):
        public = "uri/" + str(self._root_directory_uri, "ascii")
        d = self.GET("")
        def _got_welcome(page):
            html = page.replace('\n', ' ')
            connected_re = r'Connected to <span>%d</span>\s*of <span>%d</span> known storage servers' % (self.numclients, self.numclients)
            self.failUnless(re.search(connected_re, html),
                            "I didn't see the right '%s' message in:\n%s" % (connected_re, page))
            # nodeids/tubids don't have any regexp-special characters
            nodeid_re = r'<th>Node ID:</th>\s*<td title="TubID: %s">%s</td>' % (
                self.clients[0].get_long_tubid(), str(self.clients[0].get_long_nodeid(), "ascii"))
            self.failUnless(re.search(nodeid_re, html),
                            "I didn't see the right '%s' message in:\n%s" % (nodeid_re, page))
            self.failUnless("Helper: 0 active uploads" in page)
        d.addCallback(_got_welcome)
        d.addCallback(self.log, "done with _got_welcome")

        # get the welcome page from the node that uses the helper too
        d.addCallback(lambda res: do_http("get", self.helper_webish_url))
        def _got_welcome_helper(page):
            soup = BeautifulSoup(page, 'html5lib')
            assert_soup_has_tag_with_attributes(
                self, soup, u"img",
                { u"alt": u"Connected", u"src": u"img/connected-yes.png" }
            )
            self.failUnlessIn("Not running helper", page)
        d.addCallback(_got_welcome_helper)

        d.addCallback(lambda res: self.GET(public))
        d.addCallback(lambda res: self.GET(public + "/subdir1"))
        def _got_subdir1(page):
            # there ought to be an href for our file
            self.failUnlessIn('<td align="right">%d</td>' % len(self.data), page)
            self.failUnless(">mydata567</a>" in page)
        d.addCallback(_got_subdir1)
        d.addCallback(self.log, "done with _got_subdir1")
        d.addCallback(lambda res: self.GET(public + "/subdir1/mydata567"))
        def _got_data(page):
            self.failUnlessEqual(page.encode("utf-8"), self.data)
        d.addCallback(_got_data)

        # download from a URI embedded in a URL
        d.addCallback(self.log, "_get_from_uri")
        def _get_from_uri(res):
            return self.GET("uri/%s?filename=%s" % (str(self.uri, "utf-8"), "mydata567"))
        d.addCallback(_get_from_uri)
        def _got_from_uri(page):
            self.failUnlessEqual(page.encode("utf-8"), self.data)
        d.addCallback(_got_from_uri)

        # download from a URI embedded in a URL, second form
        d.addCallback(self.log, "_get_from_uri2")
        def _get_from_uri2(res):
            return self.GET("uri?uri=%s" % (str(self.uri, "utf-8"),))
        d.addCallback(_get_from_uri2)
        d.addCallback(_got_from_uri)

        # download from a bogus URI, make sure we get a reasonable error
        d.addCallback(self.log, "_get_from_bogus_uri", level=log.UNUSUAL)
        @defer.inlineCallbacks
        def _get_from_bogus_uri(res):
            d1 = self.GET("uri/%s?filename=%s"
                          % (str(self.mangle_uri(self.uri), "utf-8"), "mydata567"))
            e = yield self.assertFailure(d1, Error)
            self.assertEqual(e.status, b"410")
        d.addCallback(_get_from_bogus_uri)
        d.addCallback(self.log, "_got_from_bogus_uri", level=log.UNUSUAL)

        # upload a file with PUT
        d.addCallback(self.log, "about to try PUT")
        d.addCallback(lambda res: self.PUT(public + "/subdir3/new.txt",
                                           b"new.txt contents"))
        d.addCallback(lambda res: self.GET(public + "/subdir3/new.txt"))
        d.addCallback(self.failUnlessEqual, "new.txt contents")
        # and again with something large enough to use multiple segments,
        # and hopefully trigger pauseProducing too
        def _new_happy_semantics(ign):
            for c in self.clients:
                # these get reset somewhere? Whatever.
                c.encoding_params['happy'] = 1
        d.addCallback(_new_happy_semantics)
        d.addCallback(lambda res: self.PUT(public + "/subdir3/big.txt",
                                           b"big" * 500000)) # 1.5MB
        d.addCallback(lambda res: self.GET(public + "/subdir3/big.txt"))
        d.addCallback(lambda res: self.failUnlessEqual(len(res), 1500000))

        # can we replace files in place?
        d.addCallback(lambda res: self.PUT(public + "/subdir3/new.txt",
                                           b"NEWER contents"))
        d.addCallback(lambda res: self.GET(public + "/subdir3/new.txt"))
        d.addCallback(self.failUnlessEqual, "NEWER contents")

        # test unlinked POST
        d.addCallback(lambda res: self.POST("uri", t=b"upload",
                                            file=("new.txt", b"data" * 10000)))
        # and again using the helper, which exercises different upload-status
        # display code
        d.addCallback(lambda res: self.POST("uri", use_helper=True, t=b"upload",
                                            file=("foo.txt", b"data2" * 10000)))

        # check that the status page exists
        d.addCallback(lambda res: self.GET("status"))
        def _got_status(res):
            # find an interesting upload and download to look at. LIT files
            # are not interesting.
            h = self.clients[0].get_history()
            for ds in h.list_all_download_statuses():
                if ds.get_size() > 200:
                    self._down_status = ds.get_counter()
            for us in h.list_all_upload_statuses():
                if us.get_size() > 200:
                    self._up_status = us.get_counter()
            rs = list(h.list_all_retrieve_statuses())[0]
            self._retrieve_status = rs.get_counter()
            ps = list(h.list_all_publish_statuses())[0]
            self._publish_status = ps.get_counter()
            us = list(h.list_all_mapupdate_statuses())[0]
            self._update_status = us.get_counter()

            # and that there are some upload- and download- status pages
            return self.GET("status/up-%d" % self._up_status)
        d.addCallback(_got_status)
        def _got_up(res):
            return self.GET("status/down-%d" % self._down_status)
        d.addCallback(_got_up)
        def _got_down(res):
            return self.GET("status/mapupdate-%d" % self._update_status)
        d.addCallback(_got_down)
        def _got_update(res):
            return self.GET("status/publish-%d" % self._publish_status)
        d.addCallback(_got_update)
        def _got_publish(res):
            self.failUnlessIn("Publish Results", res)
            return self.GET("status/retrieve-%d" % self._retrieve_status)
        d.addCallback(_got_publish)
        def _got_retrieve(res):
            self.failUnlessIn("Retrieve Results", res)
        d.addCallback(_got_retrieve)

        # check that the helper status page exists
        d.addCallback(lambda res: self.GET("helper_status"))
        def _got_helper_status(res):
            self.failUnless("Bytes Fetched:" in res)
            # touch a couple of files in the helper's working directory to
            # exercise more code paths
            workdir = os.path.join(self.getdir("client0"), "helper")
            incfile = os.path.join(workdir, "CHK_incoming", "spurious")
            f = open(incfile, "wb")
            f.write(b"small file")
            f.close()
            then = time.time() - 86400*3
            now = time.time()
            os.utime(incfile, (now, then))
            encfile = os.path.join(workdir, "CHK_encoding", "spurious")
            f = open(encfile, "wb")
            f.write(b"less small file")
            f.close()
            os.utime(encfile, (now, then))
        d.addCallback(_got_helper_status)
        # and that the json form exists
        d.addCallback(lambda res: self.GET("helper_status?t=json"))
        def _got_helper_status_json(res):
            data = json.loads(res)
            self.failUnlessEqual(data["chk_upload_helper.upload_need_upload"],
                                 1)
            self.failUnlessEqual(data["chk_upload_helper.incoming_count"], 1)
            self.failUnlessEqual(data["chk_upload_helper.incoming_size"], 10)
            self.failUnlessEqual(data["chk_upload_helper.incoming_size_old"],
                                 10)
            self.failUnlessEqual(data["chk_upload_helper.encoding_count"], 1)
            self.failUnlessEqual(data["chk_upload_helper.encoding_size"], 15)
            self.failUnlessEqual(data["chk_upload_helper.encoding_size_old"],
                                 15)
        d.addCallback(_got_helper_status_json)

        # and check that client[3] (which uses a helper but does not run one
        # itself) doesn't explode when you ask for its status
        d.addCallback(lambda res: do_http("get",
                                          self.helper_webish_url + "status/"))
        def _got_non_helper_status(res):
            self.failUnlessIn("Recent and Active Operations", res)
        d.addCallback(_got_non_helper_status)

        # or for helper status with t=json
        d.addCallback(lambda res:
                      do_http("get",
                              self.helper_webish_url + "helper_status?t=json"))
        def _got_non_helper_status_json(res):
            data = json.loads(res)
            self.failUnlessEqual(data, {})
        d.addCallback(_got_non_helper_status_json)

        # see if the statistics page exists
        d.addCallback(lambda res: self.GET("statistics"))
        def _got_stats(res):
            self.failUnlessIn("Operational Statistics", res)
            self.failUnlessIn('  "downloader.files_downloaded": 5,', res)
        d.addCallback(_got_stats)
        d.addCallback(lambda res: self.GET("statistics?t=json"))
        def _got_stats_json(res):
            data = json.loads(res)
            self.failUnlessEqual(data["counters"]["uploader.files_uploaded"], 5)
            self.failUnlessEqual(data["stats"]["chk_upload_helper.upload_need_upload"], 1)
        d.addCallback(_got_stats_json)

        # TODO: mangle the second segment of a file, to test errors that
        # occur after we've already sent some good data, which uses a
        # different error path.

        # TODO: download a URI with a form
        # TODO: create a directory by using a form
        # TODO: upload by using a form on the directory page
        #    url = base + "somedir/subdir1/freeform_post!!upload"
        # TODO: delete a file by using a button on the directory page

        return d

    @defer.inlineCallbacks
    def _test_runner(self, res):
        # exercise some of the diagnostic tools in runner.py

        # find a share
        for (dirpath, dirnames, filenames) in os.walk(self.basedir):
            if "storage" not in dirpath:
                continue
            if not filenames:
                continue
            pieces = dirpath.split(os.sep)
            if (len(pieces) >= 4
                and pieces[-4] == "storage"
                and pieces[-3] == "shares"):
                # we're sitting in .../storage/shares/$START/$SINDEX , and there
                # are sharefiles here
                filename = os.path.join(dirpath, filenames[0])
                # peek at the magic to see if it is a chk share
                with open(filename, "rb") as f:
                    if ShareFile.is_valid_header(f.read(32)):
                        break
        else:
            self.fail("unable to find any uri_extension files in %r"
                      % self.basedir)
        log.msg("test_system.SystemTest._test_runner using %r" % filename)

        rc,output,err = yield run_cli("debug", "dump-share", "--offsets",
                                      unicode_to_argv(filename))
        self.failUnlessEqual(rc, 0)

        # we only upload a single file, so we can assert some things about
        # its size and shares.
        self.failUnlessIn("share filename: %s" % quote_output(abspath_expanduser_unicode(filename)), output)
        self.failUnlessIn("size: %d\n" % len(self.data), output)
        self.failUnlessIn("num_segments: 1\n", output)
        # segment_size is always a multiple of needed_shares
        self.failUnlessIn("segment_size: %d\n" % mathutil.next_multiple(len(self.data), 3), output)
        self.failUnlessIn("total_shares: 10\n", output)
        # keys which are supposed to be present
        for key in ("size", "num_segments", "segment_size",
                    "needed_shares", "total_shares",
                    "codec_name", "codec_params", "tail_codec_params",
                    #"plaintext_hash", "plaintext_root_hash",
                    "crypttext_hash", "crypttext_root_hash",
                    "share_root_hash", "UEB_hash"):
            self.failUnlessIn("%s: " % key, output)
        self.failUnlessIn("  verify-cap: URI:CHK-Verifier:", output)

        # now use its storage index to find the other shares using the
        # 'find-shares' tool
        sharedir, shnum = os.path.split(filename)
        storagedir, storage_index_s = os.path.split(sharedir)
        nodedirs = [self.getdir("client%d" % i) for i in range(self.numclients)]
        rc,out,err = yield run_cli("debug", "find-shares", storage_index_s,
                                   *nodedirs)
        self.failUnlessEqual(rc, 0)
        sharefiles = [sfn.strip() for sfn in out.splitlines()]
        self.failUnlessEqual(len(sharefiles), 10)

        # also exercise the 'catalog-shares' tool
        nodedirs = [self.getdir("client%d" % i) for i in range(self.numclients)]
        rc,out,err = yield run_cli("debug", "catalog-shares", *nodedirs)
        self.failUnlessEqual(rc, 0)
        descriptions = [sfn.strip() for sfn in out.splitlines()]
        self.failUnlessEqual(len(descriptions), 30)
        matching = [line
                    for line in descriptions
                    if line.startswith("CHK %s " % storage_index_s)]
        self.failUnlessEqual(len(matching), 10)

    def _test_cli(self, res):
        # run various CLI commands (in a thread, since they use blocking
        # network calls)

        private_uri = self._private_node.get_uri()
        client0_basedir = self.getdir("client0")

        nodeargs = [
            "--node-directory", client0_basedir,
            ]

        d = defer.succeed(None)

        # for compatibility with earlier versions, private/root_dir.cap is
        # supposed to be treated as an alias named "tahoe:". Start by making
        # sure that works, before we add other aliases.

        root_file = os.path.join(client0_basedir, "private", "root_dir.cap")
        f = open(root_file, "wb")
        f.write(private_uri)
        f.close()

        @defer.inlineCallbacks
        def run(ignored, verb, *args, **kwargs):
            rc,out,err = yield run_cli(verb, *args, nodeargs=nodeargs, **kwargs)
            defer.returnValue((out,err))

        def _check_ls(out_and_err, expected_children, unexpected_children=()):
            (out, err) = out_and_err
            self.failUnlessEqual(err, "")
            for s in expected_children:
                self.failUnless(s in out, (s,out))
            for s in unexpected_children:
                self.failIf(s in out, (s,out))

        def _check_ls_root(out_and_err):
            (out, err) = out_and_err
            self.failUnless("personal" in out)
            self.failUnless("s2-ro" in out)
            self.failUnless("s2-rw" in out)
            self.failUnlessEqual(err, "")

        # this should reference private_uri
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["personal", "s2-ro", "s2-rw"])

        d.addCallback(run, "list-aliases")
        def _check_aliases_1(out_and_err):
            (out, err) = out_and_err
            self.failUnlessEqual(err, "")
            self.failUnlessEqual(out.strip(" \n"), "tahoe: %s" % str(private_uri, "ascii"))
        d.addCallback(_check_aliases_1)

        # now that that's out of the way, remove root_dir.cap and work with
        # new files
        d.addCallback(lambda res: os.unlink(root_file))
        d.addCallback(run, "list-aliases")
        def _check_aliases_2(out_and_err):
            (out, err) = out_and_err
            self.failUnlessEqual(err, "")
            self.failUnlessEqual(out, "")
        d.addCallback(_check_aliases_2)

        d.addCallback(run, "mkdir")
        def _got_dir(out_and_err ):
            (out, err) = out_and_err
            self.failUnless(uri.from_string_dirnode(out.strip()))
            return out.strip()
        d.addCallback(_got_dir)
        d.addCallback(lambda newcap: run(None, "add-alias", "tahoe", newcap))

        d.addCallback(run, "list-aliases")
        def _check_aliases_3(out_and_err):
            (out, err) = out_and_err
            self.failUnlessEqual(err, "")
            self.failUnless("tahoe: " in out)
        d.addCallback(_check_aliases_3)

        def _check_empty_dir(out_and_err):
            (out, err) = out_and_err
            self.failUnlessEqual(out, "")
            self.failUnlessEqual(err, "")
        d.addCallback(run, "ls")
        d.addCallback(_check_empty_dir)

        def _check_missing_dir(out_and_err):
            # TODO: check that rc==2
            (out, err) = out_and_err
            self.failUnlessEqual(out, "")
            self.failUnlessEqual(err, "No such file or directory\n")
        d.addCallback(run, "ls", "bogus")
        d.addCallback(_check_missing_dir)

        files = []
        datas = []
        for i in range(10):
            fn = os.path.join(self.basedir, "file%d" % i)
            files.append(fn)
            data = b"data to be uploaded: file%d\n" % i
            datas.append(data)
            with open(fn, "wb") as f:
                f.write(data)

        def _check_stdout_against(out_and_err, filenum=None, data=None):
            (out, err) = out_and_err
            self.failUnlessEqual(err, "")
            if filenum is not None:
                self.failUnlessEqual(out, str(datas[filenum], "ascii"))
            if data is not None:
                self.failUnlessEqual(out, data)

        # test all both forms of put: from a file, and from stdin
        #  tahoe put bar FOO
        d.addCallback(run, "put", files[0], "tahoe-file0")
        def _put_out(out_and_err):
            (out, err) = out_and_err
            self.failUnless("URI:LIT:" in out, out)
            self.failUnless("201 Created" in err, err)
            uri0 = out.strip()
            return run(None, "get", uri0)
        d.addCallback(_put_out)
        d.addCallback(lambda out_err: self.failUnlessEqual(out_err[0], str(datas[0], "ascii")))

        d.addCallback(run, "put", files[1], "subdir/tahoe-file1")
        #  tahoe put bar tahoe:FOO
        d.addCallback(run, "put", files[2], "tahoe:file2")
        d.addCallback(run, "put", "--format=SDMF", files[3], "tahoe:file3")
        def _check_put_mutable(out_and_err):
            (out, err) = out_and_err
            self._mutable_file3_uri = out.strip()
        d.addCallback(_check_put_mutable)
        d.addCallback(run, "get", "tahoe:file3")
        d.addCallback(_check_stdout_against, 3)

        #  tahoe put FOO
        STDIN_DATA = "This is the file to upload from stdin."
        d.addCallback(run, "put", "-", "tahoe-file-stdin", stdin=STDIN_DATA)
        #  tahoe put tahoe:FOO
        d.addCallback(run, "put", "-", "tahoe:from-stdin",
                      stdin="Other file from stdin.")

        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["tahoe-file0", "file2", "file3", "subdir",
                                  "tahoe-file-stdin", "from-stdin"])
        d.addCallback(run, "ls", "subdir")
        d.addCallback(_check_ls, ["tahoe-file1"])

        # tahoe mkdir FOO
        d.addCallback(run, "mkdir", "subdir2")
        d.addCallback(run, "ls")
        # TODO: extract the URI, set an alias with it
        d.addCallback(_check_ls, ["subdir2"])

        # tahoe get: (to stdin and to a file)
        d.addCallback(run, "get", "tahoe-file0")
        d.addCallback(_check_stdout_against, 0)
        d.addCallback(run, "get", "tahoe:subdir/tahoe-file1")
        d.addCallback(_check_stdout_against, 1)
        outfile0 = os.path.join(self.basedir, "outfile0")
        d.addCallback(run, "get", "file2", outfile0)
        def _check_outfile0(out_and_err):
            (out, err) = out_and_err
            data = open(outfile0,"rb").read()
            self.failUnlessEqual(data, b"data to be uploaded: file2\n")
        d.addCallback(_check_outfile0)
        outfile1 = os.path.join(self.basedir, "outfile0")
        d.addCallback(run, "get", "tahoe:subdir/tahoe-file1", outfile1)
        def _check_outfile1(out_and_err):
            (out, err) = out_and_err
            data = open(outfile1,"rb").read()
            self.failUnlessEqual(data, b"data to be uploaded: file1\n")
        d.addCallback(_check_outfile1)

        d.addCallback(run, "unlink", "tahoe-file0")
        d.addCallback(run, "unlink", "tahoe:file2")
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, [], ["tahoe-file0", "file2"])

        d.addCallback(run, "ls", "-l")
        def _check_ls_l(out_and_err):
            (out, err) = out_and_err
            lines = out.split("\n")
            for l in lines:
                if "tahoe-file-stdin" in l:
                    self.failUnless(l.startswith("-r-- "), l)
                    self.failUnless(" %d " % len(STDIN_DATA) in l)
                if "file3" in l:
                    self.failUnless(l.startswith("-rw- "), l) # mutable
        d.addCallback(_check_ls_l)

        d.addCallback(run, "ls", "--uri")
        def _check_ls_uri(out_and_err):
            (out, err) = out_and_err
            lines = out.split("\n")
            for l in lines:
                if "file3" in l:
                    self.failUnless(self._mutable_file3_uri in l)
        d.addCallback(_check_ls_uri)

        d.addCallback(run, "ls", "--readonly-uri")
        def _check_ls_rouri(out_and_err):
            (out, err) = out_and_err
            lines = out.split("\n")
            for l in lines:
                if "file3" in l:
                    rw_uri = self._mutable_file3_uri
                    u = uri.from_string_mutable_filenode(rw_uri)
                    ro_uri = str(u.get_readonly().to_string(), "ascii")
                    self.failUnless(ro_uri in l)
        d.addCallback(_check_ls_rouri)


        d.addCallback(run, "mv", "tahoe-file-stdin", "tahoe-moved")
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["tahoe-moved"], ["tahoe-file-stdin"])

        d.addCallback(run, "ln", "tahoe-moved", "newlink")
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["tahoe-moved", "newlink"])

        d.addCallback(run, "cp", "tahoe:file3", "tahoe:file3-copy")
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["file3", "file3-copy"])
        d.addCallback(run, "get", "tahoe:file3-copy")
        d.addCallback(_check_stdout_against, 3)

        # copy from disk into tahoe
        d.addCallback(run, "cp", files[4], "tahoe:file4")
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["file3", "file3-copy", "file4"])
        d.addCallback(run, "get", "tahoe:file4")
        d.addCallback(_check_stdout_against, 4)

        # copy from tahoe into disk
        target_filename = os.path.join(self.basedir, "file-out")
        d.addCallback(run, "cp", "tahoe:file4", target_filename)
        def _check_cp_out(out_and_err):
            (out, err) = out_and_err
            self.failUnless(os.path.exists(target_filename))
            got = open(target_filename,"rb").read()
            self.failUnlessEqual(got, datas[4])
        d.addCallback(_check_cp_out)

        # copy from disk to disk (silly case)
        target2_filename = os.path.join(self.basedir, "file-out-copy")
        d.addCallback(run, "cp", target_filename, target2_filename)
        def _check_cp_out2(out_and_err):
            (out, err) = out_and_err
            self.failUnless(os.path.exists(target2_filename))
            got = open(target2_filename,"rb").read()
            self.failUnlessEqual(got, datas[4])
        d.addCallback(_check_cp_out2)

        # copy from tahoe into disk, overwriting an existing file
        d.addCallback(run, "cp", "tahoe:file3", target_filename)
        def _check_cp_out3(out_and_err):
            (out, err) = out_and_err
            self.failUnless(os.path.exists(target_filename))
            got = open(target_filename,"rb").read()
            self.failUnlessEqual(got, datas[3])
        d.addCallback(_check_cp_out3)

        # copy from disk into tahoe, overwriting an existing immutable file
        d.addCallback(run, "cp", files[5], "tahoe:file4")
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["file3", "file3-copy", "file4"])
        d.addCallback(run, "get", "tahoe:file4")
        d.addCallback(_check_stdout_against, 5)

        # copy from disk into tahoe, overwriting an existing mutable file
        d.addCallback(run, "cp", files[5], "tahoe:file3")
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["file3", "file3-copy", "file4"])
        d.addCallback(run, "get", "tahoe:file3")
        d.addCallback(_check_stdout_against, 5)

        # recursive copy: setup
        dn = os.path.join(self.basedir, "dir1")
        os.makedirs(dn)
        with open(os.path.join(dn, "rfile1"), "wb") as f:
            f.write(b"rfile1")
        with open(os.path.join(dn, "rfile2"), "wb") as f:
            f.write(b"rfile2")
        with open(os.path.join(dn, "rfile3"), "wb") as f:
            f.write(b"rfile3")
        sdn2 = os.path.join(dn, "subdir2")
        os.makedirs(sdn2)
        with open(os.path.join(sdn2, "rfile4"), "wb") as f:
            f.write(b"rfile4")
        with open(os.path.join(sdn2, "rfile5"), "wb") as f:
            f.write(b"rfile5")

        # from disk into tahoe
        d.addCallback(run, "cp", "-r", dn, "tahoe:")
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["dir1"])
        d.addCallback(run, "ls", "dir1")
        d.addCallback(_check_ls, ["rfile1", "rfile2", "rfile3", "subdir2"],
                      ["rfile4", "rfile5"])
        d.addCallback(run, "ls", "tahoe:dir1/subdir2")
        d.addCallback(_check_ls, ["rfile4", "rfile5"],
                      ["rfile1", "rfile2", "rfile3"])
        d.addCallback(run, "get", "dir1/subdir2/rfile4")
        d.addCallback(_check_stdout_against, data="rfile4")

        # and back out again
        dn_copy = os.path.join(self.basedir, "dir1-copy")
        d.addCallback(run, "cp", "--verbose", "-r", "tahoe:dir1", dn_copy)
        def _check_cp_r_out(out_and_err):
            (out, err) = out_and_err
            def _cmp(name):
                old = open(os.path.join(dn, name), "rb").read()
                newfn = os.path.join(dn_copy, "dir1", name)
                self.failUnless(os.path.exists(newfn))
                new = open(newfn, "rb").read()
                self.failUnlessEqual(old, new)
            _cmp("rfile1")
            _cmp("rfile2")
            _cmp("rfile3")
            _cmp(os.path.join("subdir2", "rfile4"))
            _cmp(os.path.join("subdir2", "rfile5"))
        d.addCallback(_check_cp_r_out)

        # and copy it a second time, which ought to overwrite the same files
        d.addCallback(run, "cp", "-r", "tahoe:dir1", dn_copy)

        # and again, only writing filecaps
        dn_copy2 = os.path.join(self.basedir, "dir1-copy-capsonly")
        d.addCallback(run, "cp", "-r", "--caps-only", "tahoe:dir1", dn_copy2)
        def _check_capsonly(out_and_err):
            # these should all be LITs
            (out, err) = out_and_err
            x = open(os.path.join(dn_copy2, "dir1", "subdir2", "rfile4")).read()
            y = uri.from_string_filenode(x)
            self.failUnlessEqual(y.data, b"rfile4")
        d.addCallback(_check_capsonly)

        # and tahoe-to-tahoe
        d.addCallback(run, "cp", "-r", "tahoe:dir1", "tahoe:dir1-copy")
        d.addCallback(run, "ls")
        d.addCallback(_check_ls, ["dir1", "dir1-copy"])
        d.addCallback(run, "ls", "dir1-copy/dir1")
        d.addCallback(_check_ls, ["rfile1", "rfile2", "rfile3", "subdir2"],
                      ["rfile4", "rfile5"])
        d.addCallback(run, "ls", "tahoe:dir1-copy/dir1/subdir2")
        d.addCallback(_check_ls, ["rfile4", "rfile5"],
                      ["rfile1", "rfile2", "rfile3"])
        d.addCallback(run, "get", "dir1-copy/dir1/subdir2/rfile4")
        d.addCallback(_check_stdout_against, data="rfile4")

        # and copy it a second time, which ought to overwrite the same files
        d.addCallback(run, "cp", "-r", "tahoe:dir1", "tahoe:dir1-copy")

        # tahoe_ls doesn't currently handle the error correctly: it tries to
        # JSON-parse a traceback.
##         def _ls_missing(res):
##             argv = nodeargs + ["ls", "bogus"]
##             return self._run_cli(argv)
##         d.addCallback(_ls_missing)
##         def _check_ls_missing((out,err)):
##             print("OUT", out)
##             print("ERR", err)
##             self.failUnlessEqual(err, "")
##         d.addCallback(_check_ls_missing)

        return d

    # In CI this test can be very slow, so give it a longer timeout:
    test_filesystem.timeout = 360  # type: ignore[attr-defined]


    def test_filesystem_with_cli_in_subprocess(self):
        # We do this in a separate test so that test_filesystem doesn't skip if we can't run bin/tahoe.

        d = self.set_up_nodes()
        def _new_happy_semantics(ign):
            for c in self.clients:
                c.encoding_params['happy'] = 1
        d.addCallback(_new_happy_semantics)

        def _run_in_subprocess(ignored, verb, *args, **kwargs):
            stdin = kwargs.get("stdin")
            # XXX https://tahoe-lafs.org/trac/tahoe-lafs/ticket/3548
            env = kwargs.get("env", os.environ)
            # Python warnings from the child process don't matter.
            env["PYTHONWARNINGS"] = "ignore"
            newargs = ["--node-directory", self.getdir("client0"), verb] + list(args)
            return self.run_bintahoe(newargs, stdin=stdin, env=env)

        def _check_succeeded(res):
            out, err, rc_or_sig = res
            self.failUnlessEqual(rc_or_sig, 0, str(res))

        d.addCallback(_run_in_subprocess, "create-alias", "newalias")
        d.addCallback(_check_succeeded)

        STDIN_DATA = b"This is the file to upload from stdin."
        d.addCallback(_run_in_subprocess, "put", "-", "newalias:tahoe-file", stdin=STDIN_DATA)
        d.addCallback(_check_succeeded)

        def _mv_with_http_proxy(ign):
            env = os.environ
            env['http_proxy'] = env['HTTP_PROXY'] = "http://127.0.0.0:12345"  # invalid address
            return _run_in_subprocess(None, "mv", "newalias:tahoe-file", "newalias:tahoe-moved", env=env)
        d.addCallback(_mv_with_http_proxy)
        d.addCallback(_check_succeeded)

        d.addCallback(_run_in_subprocess, "ls", "newalias:")
        def _check_ls(res):
            out, err, rc_or_sig = res
            self.failUnlessEqual(rc_or_sig, 0, str(res))
            self.failUnlessIn(b"tahoe-moved", out)
            self.failIfIn(b"tahoe-file", out)
        d.addCallback(_check_ls)
        return d

    def _test_checker(self, res):
        ut = upload.Data(b"too big to be literal" * 200, convergence=None)
        d = self._personal_node.add_file(u"big file", ut)

        d.addCallback(lambda res: self._personal_node.check(Monitor()))
        def _check_dirnode_results(r):
            self.failUnless(r.is_healthy())
        d.addCallback(_check_dirnode_results)
        d.addCallback(lambda res: self._personal_node.check(Monitor(), verify=True))
        d.addCallback(_check_dirnode_results)

        d.addCallback(lambda res: self._personal_node.get(u"big file"))
        def _got_chk_filenode(n):
            self.failUnless(isinstance(n, ImmutableFileNode))
            d = n.check(Monitor())
            def _check_filenode_results(r):
                self.failUnless(r.is_healthy())
            d.addCallback(_check_filenode_results)
            d.addCallback(lambda res: n.check(Monitor(), verify=True))
            d.addCallback(_check_filenode_results)
            return d
        d.addCallback(_got_chk_filenode)

        d.addCallback(lambda res: self._personal_node.get(u"sekrit data"))
        def _got_lit_filenode(n):
            self.failUnless(isinstance(n, LiteralFileNode))
            d = n.check(Monitor())
            def _check_lit_filenode_results(r):
                self.failUnlessEqual(r, None)
            d.addCallback(_check_lit_filenode_results)
            d.addCallback(lambda res: n.check(Monitor(), verify=True))
            d.addCallback(_check_lit_filenode_results)
            return d
        d.addCallback(_got_lit_filenode)
        return d


class Connections(SystemTestMixin, unittest.TestCase):
    FORCE_FOOLSCAP_FOR_STORAGE = True

    def test_rref(self):
        # The way the listening port is created is via
        # SameProcessStreamEndpointAssigner (allmydata.test.common), which then
        # makes an endpoint string parsed by AdoptedServerPort. The latter does
        # dup(fd), which results in the filedescriptor staying alive _until the
        # test ends_. That means that when we disown the service, we still have
        # the listening port there on the OS level! Just the resulting
        # connections aren't handled. So this test relies on aggressive
        # timeouts in the HTTP client and presumably some equivalent in
        # Foolscap, since connection refused does _not_ happen.
        self.basedir = "system/Connections/rref-foolscap-{}".format(
            self.FORCE_FOOLSCAP_FOR_STORAGE
        )
        d = self.set_up_nodes(2)
        def _start(ign):
            self.c0 = self.clients[0]
            nonclients = [s for s in self.c0.storage_broker.get_connected_servers()
                          if s.get_serverid() != self.c0.get_long_nodeid()]
            self.failUnlessEqual(len(nonclients), 1)

            self.s1 = nonclients[0]  # s1 is the server, not c0
            self.s1_storage_server = self.s1.get_storage_server()
            self.assertIsNot(self.s1_storage_server, None)
            self.assertTrue(self.s1.is_connected())
        d.addCallback(_start)

        # now shut down the server
        d.addCallback(lambda ign: self.clients[1].disownServiceParent())

        # kill any persistent http connections that might continue to work
        d.addCallback(lambda ign: self.close_idle_http_connections())

        # and wait for the client to notice
        def _poll():
            return len(self.c0.storage_broker.get_connected_servers()) == 1
        d.addCallback(lambda ign: self.poll(_poll))

        def _down(ign):
            self.assertFalse(self.s1.is_connected())
            storage_server = self.s1.get_storage_server()
            self.assertIsNot(storage_server, None)
            self.assertEqual(storage_server, self.s1_storage_server)
        d.addCallback(_down)
        return d


class HTTPSystemTest(SystemTest):
    """HTTP storage protocol variant of the system tests."""

    FORCE_FOOLSCAP_FOR_STORAGE = False



class HTTPConnections(Connections):
    """HTTP storage protocol variant of the connections tests."""
    FORCE_FOOLSCAP_FOR_STORAGE = False

