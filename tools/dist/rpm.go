package main

// The rpm format: a 96-byte lead, a signature header (digests of what follows, padded to 8
// bytes), the main header (name, version, the file list), then a gzip'd cpio payload.
// Headers are an index of (tag, type, offset, count) entries over a data store, sealed
// by a region tag that rpm uses to tell the signed part from what it added later.

import (
	"bytes"
	"crypto/md5"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"path"
	"sort"
)

const (
	rpmInt16       = 3
	rpmInt32       = 4
	rpmString      = 6
	rpmBin         = 7
	rpmStringArray = 8
	rpmI18NString  = 9

	rpmTagHeaderSignatures = 62
	rpmTagHeaderImmutable  = 63
)

type rpmEntry struct {
	tag, kind uint32
	count     uint32
	data      []byte
}

// rpmHeader collects a header's entries by tag.
type rpmHeader map[uint32]rpmEntry

func (h rpmHeader) str(tag uint32, value string) {
	h[tag] = rpmEntry{tag: tag, kind: rpmString, count: 1, data: append([]byte(value), 0)}
}

func (h rpmHeader) i18n(tag uint32, value string) {
	h[tag] = rpmEntry{tag: tag, kind: rpmI18NString, count: 1, data: append([]byte(value), 0)}
}

func (h rpmHeader) strs(tag uint32, values ...string) {
	var data []byte
	for _, value := range values {
		data = append(append(data, value...), 0)
	}
	h[tag] = rpmEntry{tag: tag, kind: rpmStringArray, count: uint32(len(values)), data: data}
}

func (h rpmHeader) int32s(tag uint32, values ...uint32) {
	data := make([]byte, 4*len(values))
	for index, value := range values {
		binary.BigEndian.PutUint32(data[4*index:], value)
	}
	h[tag] = rpmEntry{tag: tag, kind: rpmInt32, count: uint32(len(values)), data: data}
}

func (h rpmHeader) int16s(tag uint32, values ...uint16) {
	data := make([]byte, 2*len(values))
	for index, value := range values {
		binary.BigEndian.PutUint16(data[2*index:], value)
	}
	h[tag] = rpmEntry{tag: tag, kind: rpmInt16, count: uint32(len(values)), data: data}
}

func (h rpmHeader) bin(tag uint32, value []byte) {
	h[tag] = rpmEntry{tag: tag, kind: rpmBin, count: uint32(len(value)), data: value}
}

// bytes lays the header out, sealed with region as its first entry and its trailer last.
func (h rpmHeader) bytes(region uint32) []byte {
	tags := make([]int, 0, len(h))
	for tag := range h {
		tags = append(tags, int(tag))
	}
	sort.Ints(tags)
	var store bytes.Buffer
	index := make([][16]byte, 0, len(tags)+1)
	entry := func(tag, kind, offset, count uint32) [16]byte {
		var raw [16]byte
		binary.BigEndian.PutUint32(raw[0:], tag)
		binary.BigEndian.PutUint32(raw[4:], kind)
		binary.BigEndian.PutUint32(raw[8:], offset)
		binary.BigEndian.PutUint32(raw[12:], count)
		return raw
	}
	index = append(index, [16]byte{})
	for _, tag := range tags {
		item := h[uint32(tag)]
		// Numbers are aligned to their size within the store.
		align := map[uint32]int{rpmInt16: 2, rpmInt32: 4}[item.kind]
		for align > 0 && store.Len()%align != 0 {
			store.WriteByte(0)
		}
		index = append(index, entry(item.tag, item.kind, uint32(store.Len()), item.count))
		store.Write(item.data)
	}
	// The region's trailer points back over the whole index (as a negative offset).
	trailer := entry(region, rpmBin, uint32(-int32(16*len(index))), 16)
	index[0] = entry(region, rpmBin, uint32(store.Len()), 16)
	store.Write(trailer[:])
	var out bytes.Buffer
	out.Write([]byte{0x8e, 0xad, 0xe8, 0x01, 0, 0, 0, 0})
	binary.Write(&out, binary.BigEndian, uint32(len(index)))
	binary.Write(&out, binary.BigEndian, uint32(store.Len()))
	for _, raw := range index {
		out.Write(raw[:])
	}
	out.Write(store.Bytes())
	return out.Bytes()
}

// cpio writes the payload in the "newc" format, with names relative to / as rpm expects.
func (p *Package) cpio() []byte {
	var out bytes.Buffer
	pad := func() {
		for out.Len()%4 != 0 {
			out.WriteByte(0)
		}
	}
	write := func(inode int, mode int64, mtime int64, name string, data []byte) {
		fmt.Fprintf(&out, "070701%08x%08x%08x%08x%08x%08x%08x%08x%08x%08x%08x%08x%08x",
			inode, mode, 0, 0, 1, mtime, len(data), 0, 0, 0, 0, len(name)+1, 0)
		out.WriteString(name)
		out.WriteByte(0)
		pad()
		out.Write(data)
		pad()
	}
	for index, file := range p.files() {
		write(index+1, 0o100000|file.Mode, p.Built.Unix(), "."+file.Path, file.Data)
	}
	write(0, 0, 0, "TRAILER!!!", nil)
	return out.Bytes()
}

func (p *Package) rpm(arch string) ([]byte, error) {
	files := p.files()
	payload := p.cpio()
	compressed := gzipped(payload)

	h := rpmHeader{}
	h.strs(100, "C") // HEADERI18NTABLE: the one locale of the I18N strings
	h.str(1000, p.Name)
	h.str(1001, p.Version)
	h.str(1002, fmt.Sprint(p.Release))
	h.i18n(1004, p.Summary)
	h.i18n(1005, p.Description)
	h.int32s(1006, uint32(p.Built.Unix()))
	h.str(1007, "tools-dist")
	h.int32s(1009, uint32(p.installedSize()))
	h.str(1014, license)
	h.i18n(1016, "Development/Tools")
	h.str(1020, homepage)
	h.str(1021, "linux")
	h.str(1022, arch)
	h.str(1064, "4.16.0") // RPMVERSION: the format this follows
	h.str(1124, "cpio")
	h.str(1125, "gzip")
	h.str(1126, "9")
	h.str(1132, arch+"-linux")

	var sizes, mtimes, flags, devices, inodes, dirIndexes []uint32
	var modes, rdevs []uint16
	var digests, links, users, groups, langs, basenames, dirnames []string
	dirIndex := map[string]uint32{}
	for index, file := range files {
		dir, base := path.Split(file.Path)
		if _, ok := dirIndex[dir]; !ok {
			dirIndex[dir] = uint32(len(dirnames))
			dirnames = append(dirnames, dir)
		}
		sum := sha256.Sum256(file.Data)
		sizes = append(sizes, uint32(len(file.Data)))
		mtimes = append(mtimes, uint32(p.Built.Unix()))
		flags = append(flags, 0)
		devices = append(devices, 1)
		inodes = append(inodes, uint32(index+1))
		dirIndexes = append(dirIndexes, dirIndex[dir])
		modes = append(modes, uint16(0o100000|file.Mode))
		rdevs = append(rdevs, 0)
		digests = append(digests, hex.EncodeToString(sum[:]))
		links = append(links, "")
		users = append(users, "root")
		groups = append(groups, "root")
		langs = append(langs, "")
		basenames = append(basenames, base)
	}
	h.int32s(1028, sizes...)
	h.int16s(1030, modes...)
	h.int16s(1033, rdevs...)
	h.int32s(1034, mtimes...)
	h.strs(1035, digests...)
	h.strs(1036, links...)
	h.int32s(1037, flags...)
	h.strs(1039, users...)
	h.strs(1040, groups...)
	h.int32s(1095, devices...)
	h.int32s(1096, inodes...)
	h.strs(1097, langs...)
	h.int32s(1116, dirIndexes...)
	h.strs(1117, basenames...)
	h.strs(1118, dirnames...)
	h.int32s(5011, 8) // FILEDIGESTALGO: SHA-256

	// What it provides, and the rpmlib features its layout needs.
	h.strs(1047, p.Name)
	h.int32s(1112, 8) // RPMSENSE_EQUAL
	h.strs(1113, fmt.Sprintf("%s-%d", p.Version, p.Release))
	const rpmlib = 0x01000000 | 0x08 | 0x02 // RPMLIB, EQUAL, LESS
	h.strs(1049, "rpmlib(CompressedFileNames)", "rpmlib(FileDigests)", "rpmlib(PayloadFilesHavePrefix)")
	h.int32s(1048, rpmlib, rpmlib, rpmlib)
	h.strs(1050, "3.0.4-1", "4.6.0-1", "4.0-1")

	payloadSum := sha256.Sum256(compressed)
	payloadAlt := sha256.Sum256(payload)
	h.strs(5092, hex.EncodeToString(payloadSum[:])) // PAYLOADDIGEST
	h.int32s(5093, 8)                               // PAYLOADDIGESTALGO: SHA-256
	h.strs(5097, hex.EncodeToString(payloadAlt[:])) // PAYLOADDIGESTALT: of the uncompressed payload
	header := h.bytes(rpmTagHeaderImmutable)

	sha1Sum := sha1.Sum(header)
	sha256Sum := sha256.Sum256(header)
	md5Sum := md5.New()
	md5Sum.Write(header)
	md5Sum.Write(compressed)
	sig := rpmHeader{}
	sig.str(269, hex.EncodeToString(sha1Sum[:]))   // SHA1 of the header
	sig.str(273, hex.EncodeToString(sha256Sum[:])) // SHA256 of the header
	sig.int32s(1000, uint32(len(header)+len(compressed)))
	sig.bin(1004, md5Sum.Sum(nil))
	sig.int32s(1007, uint32(len(payload)))
	signature := sig.bytes(rpmTagHeaderSignatures)

	var out bytes.Buffer
	lead := make([]byte, 96)
	copy(lead, []byte{0xed, 0xab, 0xee, 0xdb, 3, 0})
	binary.BigEndian.PutUint16(lead[6:], 0) // binary package
	binary.BigEndian.PutUint16(lead[8:], 1)
	copy(lead[10:75], fmt.Sprintf("%s-%s-%d", p.Name, p.Version, p.Release))
	binary.BigEndian.PutUint16(lead[76:], 1) // Linux
	binary.BigEndian.PutUint16(lead[78:], 5) // a header-style signature
	out.Write(lead)
	out.Write(signature)
	for out.Len()%8 != 0 {
		out.WriteByte(0)
	}
	out.Write(header)
	out.Write(compressed)
	return out.Bytes(), nil
}
