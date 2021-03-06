from abc import ABCMeta
import construct
import hashlib
import logging
import macho_cs

log = logging.getLogger(__name__)

# See the documentation for an explanation of how
# CodeDirectory slots work.
class CodeDirectorySlot(object):
    __metaclass__ = ABCMeta
    offset = None

    def __init__(self, codesig):
        self.codesig = codesig

    def get_hash(self, hash_type):
        if hash_type == macho_cs.SHA1_HASHTYPE:
            return hashlib.sha1(self.get_contents()).digest()
        elif hash_type == macho_cs.SHA256_HASHTYPE:
            return hashlib.sha256(self.get_contents()).digest()
        else:
            raise ValueError("unknown hash type %s" % (hash_type,))


class EntitlementsSlot(CodeDirectorySlot):
    offset = -5

    def get_contents(self):
        return self.codesig.get_blob_data('CSMAGIC_ENTITLEMENT')


class ApplicationSlot(CodeDirectorySlot):
    offset = -4

    def get_hash(self, hash_type):
        return '\x00' * (20 if hash_type == macho_cs.SHA1_HASHTYPE else 32)


class ResourceDirSlot(CodeDirectorySlot):
    offset = -3

    def __init__(self, codesig, seal_path):
        super(ResourceDirSlot, self).__init__(codesig)
        self.seal_path = seal_path

    def get_contents(self):
        return open(self.seal_path, "rb").read()


class RequirementsSlot(CodeDirectorySlot):
    offset = -2

    def get_contents(self):
        return self.codesig.get_blob_data('CSMAGIC_REQUIREMENTS')


class InfoSlot(CodeDirectorySlot):
    offset = -1

    def __init__(self, info_path):
        self.info_path = info_path

    def get_contents(self):
        return open(self.info_path, "rb").read()


class MultipleEntriesException(Exception):
    pass

# Represents a code signature object, aka the LC_CODE_SIGNATURE,
# within the Signable
class Codesig(object):
    """ wrapper around construct for code signature """
    def __init__(self, signable, data):
        self.signable = signable
        self.construct = macho_cs.Blob.parse(data)

    def build_data(self):
        return macho_cs.Blob.build(self.construct)

    def get_blobs(self, magic):
        return [index.blob
                for index in self.construct.data.BlobIndex
                if index.blob.magic == magic]

    def get_blob(self, magic):
        blobs = self.get_blobs(magic)
        if not blobs:
            raise KeyError(magic)
        if len(blobs) > 1:
            raise MultipleEntriesException(len(blobs))
        return blobs[0]

    def get_blob_data(self, magic):
        """ convenience method, if we just want the data """
        blob = self.get_blob(magic)
        return macho_cs.Blob_.build(blob)

    def set_entitlements(self, entitlements_bytes):
        # log.debug("entitlements:")
        try:
            entitlements = self.get_blob('CSMAGIC_ENTITLEMENT')
        except KeyError:
            log.debug("no entitlements found")
        else:
            # make entitlements data if slot was found
            # libraries do not have entitlements data
            # so this is actually a difference between libs and apps
            # entitlements_data = macho_cs.Blob_.build(entitlements)
            # log.debug(hashlib.sha1(entitlements_data).hexdigest())

            entitlements.bytes = entitlements_bytes
            entitlements.length = len(entitlements_bytes) + 8
            # entitlements_data = macho_cs.Blob_.build(entitlements)
            # log.debug(hashlib.sha1(entitlements_data).hexdigest())

    def set_requirements(self, signer):

        # log.debug("requirements:")
        requirements = self.get_blob('CSMAGIC_REQUIREMENTS')
        # requirements_data = macho_cs.Blob_.build(requirements)
        # log.debug(hashlib.sha1(requirements_data).hexdigest())

        if signer.is_adhoc():
            log.debug("Ad hoc -- using empty requirement set")
            requirements.count = 0
            return


        signer_cn = signer.get_common_name()

        # this is for convenience, a reference to the first blob
        # structure within requirements, which contains the data
        # we are going to change
        req_blob_0 = requirements.data.BlobIndex[0].blob
        req_blob_0_original_length = req_blob_0.length

        if self.signable.get_changed_bundle_id():
            # Set the bundle id if it changed
            try:
                bundle_struct = req_blob_0.data.expr.data[0].data
                bundle_struct.data = self.signable.get_changed_bundle_id()
                bundle_struct.length = len(bundle_struct.data)
            except Exception:
                log.debug("could not set bundle id")

        try:
            cn = req_blob_0.data.expr.data[1].data[1].data[0].data[2].Data
        except Exception:
            log.debug("no signer CN rule found in requirements")
            log.debug(requirements)
        else:
            # if we could find a signer CN rule, make requirements.

            # first, replace old signer CN with our own
            cn.data = signer_cn
            cn.length = len(cn.data)

            # req_blob_0 contains that CN, so rebuild it, and get what
            # the length is now
            req_blob_0.bytes = macho_cs.Requirement.build(req_blob_0.data)
            req_blob_0.length = len(req_blob_0.bytes) + 8

            # fix offsets of later blobs in requirements
            offset_delta = req_blob_0.length - req_blob_0_original_length
            for bi in requirements.data.BlobIndex[1:]:
                bi.offset += offset_delta

            # rebuild requirements, and set length for whole thing
            requirements.bytes = macho_cs.Entitlements.build(requirements.data)
            requirements.length = len(requirements.bytes) + 8

        # then rebuild the whole data, but just to show the digest...?
        # requirements_data = macho_cs.Blob_.build(requirements)
        # log.debug(hashlib.sha1(requirements_data).hexdigest())

    def get_codedirectories(self):
        return self.get_blobs('CSMAGIC_CODEDIRECTORY')

    def get_codedirectory_hashes(self):
        cd_datas = [(cd, macho_cs.Blob_.build(cd))
                    for cd in self.get_codedirectories()]
        return [{macho_cs.SHA1_HASHTYPE: hashlib.sha1(data).digest(),
                 macho_cs.SHA256_HASHTYPE: hashlib.sha256(data).digest(),
                 'hashType': cd.data.hashType}
                for cd, data in cd_datas]

    def get_codedirectory_hash_index(self, codedirectory, slot):
        """ The slots have negative offsets, because they start from the 'top'.
            So to get the actual index, we add it to the length of the
            slots. """
        return slot.offset + codedirectory.data.nSpecialSlots

    def has_codedirectory_slot(self, codedirectory, slot):
        """ Some dylibs have all 5 slots, even though technically they only need
            the first 2. If this dylib only has 2 slots, some of the calculated
            indices for slots will be negative. This means we don't do
            those slots when resigning (for dylibs, they don't add any
            security anyway) """
        return self.get_codedirectory_hash_index(codedirectory, slot) >= 0

    def fill_codedirectory_slot(self, codedirectory, slot):
        if self.signable.should_fill_slot(self, slot):
            index = self.get_codedirectory_hash_index(codedirectory, slot)
            codedirectory.data.hashes[index] = slot.get_hash(codedirectory.data.hashType)


    def set_codedirectory(self, seal_path, info_path, signer):
        for cd in self.get_codedirectories():
            if self.has_codedirectory_slot(cd, EntitlementsSlot) and not signer.is_adhoc():
                self.fill_codedirectory_slot(cd, EntitlementsSlot(self))

            if self.has_codedirectory_slot(cd, ResourceDirSlot):
                self.fill_codedirectory_slot(cd, ResourceDirSlot(self, seal_path))

            if self.has_codedirectory_slot(cd, RequirementsSlot):
                self.fill_codedirectory_slot(cd, RequirementsSlot(self))

            if self.has_codedirectory_slot(cd, ApplicationSlot):
                self.fill_codedirectory_slot(cd, ApplicationSlot(self))

            if self.has_codedirectory_slot(cd, InfoSlot):
                self.fill_codedirectory_slot(cd, InfoSlot(info_path))

        for cd in self.get_codedirectories():
            cd.data.teamID = signer.get_team_id()

            changed_bundle_id = self.signable.get_changed_bundle_id()
            if changed_bundle_id:
                offset_change = len(changed_bundle_id) - len(cd.data.ident)
                cd.data.ident = changed_bundle_id
                cd.data.hashOffset += offset_change
                if cd.data.teamIDOffset is None:
                    cd.data.teamIDOffset = offset_change
                else:
                    cd.data.teamIDOffset += offset_change
                cd.length += offset_change

            cd.bytes = macho_cs.CodeDirectory.build(cd.data)
            # open("cdrip", "wb").write(cd_data)
            log.debug("CDHash sha1:" + hashlib.sha1(cd.bytes).hexdigest())
            log.debug("CDHash sha256:" + hashlib.sha256(cd.bytes).hexdigest())

    def set_signature(self, signer):
        # cds = self.get_codedirectories()
        # print "ABOUT TO SIGN", len(cds), "cds"
        # for i, cd in enumerate(cds):
        #     print("cd", i)
        #     for k, v in cd.iteritems():
        #         if k == "data":
        #             print "data:"
        #             for k, v in v.iteritems():
        #                 if k == "hashes":
        #                     print " ", k, v[:5]
        #                     print " ", "...", len(v)
        #                 elif k != "bytes":
        #                     print " ", k, v
        #         elif k != "bytes":
        #             print k, v

        # TODO how do we even know this blobwrapper contains the signature?
        # seems like this is a coincidence of the structure, where
        # it's the only blobwrapper at that level...
        # log.debug("sig:")
        sigwrapper = self.get_blob('CSMAGIC_BLOBWRAPPER')
        oldsig = sigwrapper.bytes.value
        # signer._log_parsed_asn1(sigwrapper.data.data.value)
        # open("sigrip.der", "wb").write(sigwrapper.data.data.value)

        sig = signer.sign(oldsig, self.get_codedirectory_hashes())
        # log.debug("sig len: {0}".format(len(sig)))
        # log.debug("old sig len: {0}".format(len(oldsig)))
        # open("my_sigrip.der", "wb").write(sig)
        sigwrapper.data = construct.Container(data=sig)
        # signer._log_parsed_asn1(sig)
        # sigwrapper.data = construct.Container(data="hahaha")
        sigwrapper.length = len(sigwrapper.data.data) + 8
        sigwrapper.bytes = sigwrapper.data.data
        # log.debug(len(sigwrapper.bytes))

    def update_offsets(self):
        # update section offsets, to account for any length changes
        offset = self.construct.data.BlobIndex[0].offset
        for blob in self.construct.data.BlobIndex:
            blob.offset = offset
            offset += len(macho_cs.Blob.build(blob.blob))

        superblob = macho_cs.SuperBlob.build(self.construct.data)
        self.construct.length = len(superblob) + 8
        self.construct.bytes = superblob

    def resign(self, bundle, signer):
        """ Do the actual signing. Create the structure and then update all the
            byte offsets """

        # TODO - the way entitlements are handled is a bit of a code smell
        # We're doing a hasattr on entitlements_path to detect whether it's a top-level app.
        #      maybe - isinstance(App, bundle) ?
        # Also we don't actually need a path to entitlements, just the entitlements data as bytes.
        if hasattr(bundle, 'entitlements') and bundle.entitlements is not None:
            self.set_entitlements(bundle.entitlements)
        self.set_requirements(signer)
        # See docs/codedirectory.rst for some notes on optional hashes
        self.set_codedirectory(bundle.seal_path, bundle.info_path, signer)
        self.set_signature(signer)
        self.update_offsets()

    # TODO make this optional, in case we want to check hashes or something
    # log.debug(hashes)
    # cd = codesig_cons.data.BlobIndex[0].blob.data
    # end_offset = arch_macho.macho_start + cd.codeLimit
    # start_offset = ((end_offset + 0xfff) & ~0xfff) - (cd.nCodeSlots * 0x1000)

    # for i in xrange(cd.nSpecialSlots):
    #    expected = cd.hashes[i]
    #    log.debug("special exp=%s" % expected.encode('hex'))

    # for i in xrange(cd.nCodeSlots):
    #     expected = cd.hashes[cd.nSpecialSlots + i]
    #     f.seek(start_offset + 0x1000 * i)
    #     actual_data = f.read(min(0x1000, end_offset - f.tell()))
    #     actual = hashlib.sha1(actual_data).digest()
    #     log.debug('[%s] exp=%s act=%s' % ()
    #         ('bad', 'ok ')[expected == actual],
    #         expected.encode('hex'),
    #         actual.encode('hex')
    #     )
