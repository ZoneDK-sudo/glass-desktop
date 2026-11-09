"""Pure-Python LZ4 block compressor, used when the lz4 package can't be installed.
Produces a standard raw LZ4 block (no size prefix), same as lz4.block.compress(store_size=False)."""


def compress(src: bytes) -> bytes:
    n = len(src)
    out = bytearray()
    table = {}
    anchor = 0
    i = 0
    limit = n - 12        # last match must start 12 bytes before the end
    copy_limit = n - 5    # last 5 bytes are always literals
    mv = memoryview(src)

    def put_len(length):
        while length >= 255:
            out.append(255)
            length -= 255
        out.append(length)

    while i <= limit:
        key = mv[i:i + 4].tobytes()
        ref = table.get(key)
        table[key] = i
        if ref is None or i - ref > 65535:
            i += 1
            continue
        # extend the match
        j = i + 4
        r = ref + 4
        while j < copy_limit and src[j] == src[r]:
            j += 1
            r += 1
        lit = i - anchor
        mlen = j - i - 4
        out.append((min(lit, 15) << 4) | min(mlen, 15))
        if lit >= 15:
            put_len(lit - 15)
        out += src[anchor:i]
        dist = i - ref
        out.append(dist & 0xFF)
        out.append(dist >> 8)
        if mlen >= 15:
            put_len(mlen - 15)
        i = j
        anchor = j
    lit = n - anchor
    out.append(min(lit, 15) << 4)
    if lit >= 15:
        put_len(lit - 15)
    out += src[anchor:]
    return bytes(out)
