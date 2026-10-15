# hydra-rpc source module: ed25519 (bundled into ./hydra-rpc by build.py)
# Minimal, dependency-free Ed25519 (RFC 8032) used to verify signed update
# checksums. Points use extended homogeneous coordinates, so a full
# verification needs no field inversions (fast enough for a one-shot update).
ED25519_SEED_SIZE = 32
ED25519_PUBLIC_KEY_SIZE = 32
ED25519_SIGNATURE_SIZE = 64

_ED_P = 2**255 - 19
_ED_L = 2**252 + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P
_ED_D2 = (2 * _ED_D) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)
_ED_IDENTITY = (0, 1, 1, 0)


def _ed_bit(data, index):
    return (data[index >> 3] >> (index & 7)) & 1


def _ed_inv(value):
    return pow(value, _ED_P - 2, _ED_P)


def _ed_hint(data):
    digest = hashlib.sha512(data).digest()
    return sum(2**i * _ed_bit(digest, i) for i in range(512))


def _ed_xrecover(y):
    xx = (y * y - 1) * _ed_inv(_ED_D * y * y + 1)
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * _ED_I) % _ED_P
    if x % 2 != 0:
        x = _ED_P - x
    return x


_ED_BY = (4 * _ed_inv(5)) % _ED_P
_ED_BX = _ed_xrecover(_ED_BY)
_ED_BASE = (_ED_BX, _ED_BY, 1, _ED_BX * _ED_BY % _ED_P)


def _ed_add(point1, point2):
    # Unified (complete) twisted-Edwards addition: valid for doubling too.
    x1, y1, z1, t1 = point1
    x2, y2, z2, t2 = point2
    a = (y1 - x1) * (y2 - x2) % _ED_P
    b = (y1 + x1) * (y2 + x2) % _ED_P
    c = t1 * _ED_D2 % _ED_P * t2 % _ED_P
    dd = z1 * 2 * z2 % _ED_P
    e = (b - a) % _ED_P
    f = (dd - c) % _ED_P
    g = (dd + c) % _ED_P
    h = (b + a) % _ED_P
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_scalarmult(point, scalar):
    result = _ED_IDENTITY
    addend = point
    while scalar > 0:
        if scalar & 1:
            result = _ed_add(result, addend)
        addend = _ed_add(addend, addend)
        scalar >>= 1
    return result


def _ed_point_eq(point1, point2):
    x1, y1, z1, _ = point1
    x2, y2, z2, _ = point2
    return (x1 * z2 - x2 * z1) % _ED_P == 0 and (y1 * z2 - y2 * z1) % _ED_P == 0


def _ed_encode_point(point):
    x, y, z, _ = point
    zinv = _ed_inv(z)
    aff_x = x * zinv % _ED_P
    aff_y = y * zinv % _ED_P
    return (aff_y | ((aff_x & 1) << 255)).to_bytes(32, "little")


def _ed_decode_point(data):
    if len(data) != 32:
        raise ValueError("Ed25519 point must be 32 bytes")
    value = int.from_bytes(data, "little")
    y = value & ((1 << 255) - 1)
    if y >= _ED_P:
        raise ValueError("Ed25519 point is not canonical")
    x = _ed_xrecover(y)
    if (x & 1) != ((value >> 255) & 1):
        x = _ED_P - x
    if (-x * x + y * y - 1 - _ED_D * x * x * y * y) % _ED_P != 0:
        raise ValueError("Ed25519 point is not on the curve")
    return (x, y, 1, x * y % _ED_P)


def _ed_scalar_from_digest(digest):
    # RFC 8032 clamp: clear the low 3 bits, clear bit 255, set bit 254.
    return 2**254 + sum(2**i * _ed_bit(digest, i) for i in range(3, 254))


def ed25519_public_key(seed):
    if len(seed) != ED25519_SEED_SIZE:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    return _ed_encode_point(_ed_scalarmult(_ED_BASE, _ed_scalar_from_digest(digest)))


def ed25519_sign(message, seed):
    if len(seed) != ED25519_SEED_SIZE:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = _ed_scalar_from_digest(digest)
    public_key = _ed_encode_point(_ed_scalarmult(_ED_BASE, scalar))
    r = _ed_hint(digest[32:64] + message)
    encoded_r = _ed_encode_point(_ed_scalarmult(_ED_BASE, r))
    k = _ed_hint(encoded_r + public_key + message)
    s = (r + k * scalar) % _ED_L
    return encoded_r + s.to_bytes(32, "little")


def ed25519_verify(signature, message, public_key):
    if len(signature) != ED25519_SIGNATURE_SIZE or len(public_key) != ED25519_PUBLIC_KEY_SIZE:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _ED_L:  # reject non-canonical scalars (malleable signatures)
        return False
    try:
        r = _ed_decode_point(signature[:32])
        a = _ed_decode_point(public_key)
    except ValueError:
        return False
    k = _ed_hint(signature[:32] + public_key + message)
    return _ed_point_eq(_ed_scalarmult(_ED_BASE, s), _ed_add(r, _ed_scalarmult(a, k)))
