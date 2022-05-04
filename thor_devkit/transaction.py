"""
Transaction class defines VeChain's multi-clause transaction (tx).

This module defines data structure of a tx, and the encoding/decoding of tx data.
"""
import sys
from copy import deepcopy
from typing import Any, Dict, List, Optional, Sequence, Union

import voluptuous
from voluptuous import REMOVE_EXTRA, Schema

from .cry import address, blake2b256, secp256k1
from .deprecation import deprecated_to_property
from .exceptions import BadTransaction
from .rlp import (
    BaseWrapper,
    BlobKind,
    BytesKind,
    CompactFixedBlobKind,
    ComplexCodec,
    DictWrapper,
    HomoListWrapper,
    NumericKind,
    OptionalFixedBlobKind,
    ScalarKind,
)
from .utils import _AnyBytes

if sys.version_info < (3, 8):
    from typing_extensions import Final, TypedDict
else:
    from typing import Final, TypedDict
if sys.version_info < (3, 11):
    from typing_extensions import NotRequired
else:
    from typing import NotRequired


# Kind Definitions
# Used for VeChain's "reserved features" kind.
FeaturesKind: Final = NumericKind(4)

# Unsigned/Signed RLP Wrapper.
_params: Final[Dict[str, Union[BaseWrapper, ScalarKind[Any]]]] = {
    "chainTag": NumericKind(1),
    "blockRef": CompactFixedBlobKind(8),
    "expiration": NumericKind(4),
    "clauses": HomoListWrapper(
        DictWrapper(
            {
                "to": OptionalFixedBlobKind(20),
                "value": NumericKind(32),
                "data": BlobKind(),
            }
        )
    ),
    "gasPriceCoef": NumericKind(1),
    "gas": NumericKind(8),
    "dependsOn": OptionalFixedBlobKind(32),
    "nonce": NumericKind(8),
    "reserved": HomoListWrapper(codec=BytesKind()),
}

# Unsigned Tx Wrapper
UnsignedTxWrapper: Final = DictWrapper(_params)

# Signed Tx Wrapper
SignedTxWrapper: Final = DictWrapper({**_params, "signature": BytesKind()})


class ClauseT(TypedDict):
    to: Optional[str]
    value: Union[str, int]
    data: str


CLAUSE: Final = Schema(
    {
        # Destination contract address, or set to None to create contract.
        "to": voluptuous.Any(str, None),
        "value": voluptuous.Any(str, int),  # VET to pass to the call.
        "data": str,
    },
    required=True,
    extra=REMOVE_EXTRA,
)


class ReservedT(TypedDict, total=False):
    features: int
    unused: Sequence[_AnyBytes]


RESERVED: Final = Schema(
    {
        voluptuous.Optional("features"): int,  # int.
        voluptuous.Optional("unused"): [voluptuous.Any(bytes, bytearray)],
        # "unused" In TypeScript version is of type: Buffer[]
        # Buffer itself is "byte[]",
        # which is equivalent to "bytes"/"bytearray" in Python.
        # So Buffer[] is "[bytes]"/"[bytearray]" in Python.
    },
    required=True,
    extra=REMOVE_EXTRA,
)


class TransactionBodyT(TypedDict):
    chainTag: int  # noqa: N815
    blockRef: str  # noqa: N815
    expiration: int
    clauses: Sequence[ClauseT]
    gasPriceCoef: int  # noqa: N815
    gas: Union[int, str]
    dependsOn: Optional[str]  # noqa: N815
    nonce: Union[int, str]
    reserved: NotRequired[ReservedT]


BODY: Final = Schema(
    {
        "chainTag": int,
        "blockRef": str,
        "expiration": int,
        "clauses": [CLAUSE],
        "gasPriceCoef": int,
        "gas": voluptuous.Any(str, int),
        "dependsOn": voluptuous.Any(str, None),
        "nonce": voluptuous.Any(str, int),
        voluptuous.Optional("reserved"): RESERVED,
    },
    required=True,
    extra=REMOVE_EXTRA,
)


def data_gas(data: str) -> int:
    """
    Calculate the gas the data will consume.

    Parameters
    ----------
    data : str
        '0x...' style hex string.
    """
    Z_GAS = 4
    NZ_GAS = 68

    return sum(
        Z_GAS if odd == even == "0" else NZ_GAS
        for odd, even in zip(data[2::2], data[3::2])
    )


def intrinsic_gas(clauses: Sequence[ClauseT]) -> int:
    """
    Calculate roughly the gas from a list of clauses.

    Parameters
    ----------
    clauses : List
        A list of clauses (in dict format).

    Returns
    -------
    int
        The sum of gas.
    """
    TX_GAS = 5000
    CLAUSE_GAS = 16000
    CLAUSE_CONTRACT_CREATION = 48000

    if not clauses:
        return TX_GAS + CLAUSE_GAS

    sum_total = TX_GAS
    for clause in clauses:
        if clause["to"]:  # contract create.
            sum_total += CLAUSE_GAS
        else:
            sum_total += CLAUSE_CONTRACT_CREATION
        sum_total += data_gas(clause["data"])

    return sum_total


def right_trim_empty_bytes(m_list: Sequence[_AnyBytes]) -> List[bytes]:
    """Given a list of bytes, remove the b'' from the tail of the list."""
    rightmost_none_empty = next(
        (idx for idx, item in enumerate(reversed(m_list)) if item), None
    )

    if rightmost_none_empty is None:
        return []

    return list(m_list[: len(m_list) - rightmost_none_empty])


class Transaction:
    # The reserved feature of delegated (vip-191) is 1.
    DELEGATED_MASK: Final = 1
    _signature: Optional[bytes] = None

    def __init__(self, body: TransactionBodyT) -> None:
        """Construct a transaction from a given body."""
        self.body = BODY(body)

    def get_body(self, as_copy: bool = True) -> TransactionBodyT:
        """
        Get a dict of the body represents the transaction.
        If as_copy, return a newly created dict.
        If not, return the body used in this Transaction object.

        Parameters
        ----------
        as_copy : bool, optional
            Return a new dict clone of the body, by default True
        """
        if as_copy:
            return deepcopy(self.body)
        else:
            return self.body

    def _encode_reserved(self) -> List[bytes]:
        reserved = self.body.get("reserved", {})
        f = reserved.get("features") or 0
        unused: List[bytes] = reserved.get("unused", []) or []
        m_list = [FeaturesKind.serialize(f)] + unused

        return right_trim_empty_bytes(m_list)
        # return m_list

    def get_signing_hash(self, delegate_for: Optional[str] = None) -> bytes:
        buff = self.encode(force_unsigned=True)
        h, _ = blake2b256([buff])

        if delegate_for:
            if not address.is_address(delegate_for):
                raise ValueError("delegate_for should be an address type.")
            x, _ = blake2b256([h, bytes.fromhex(delegate_for[2:])])
            return x

        return h

    @property
    def intrinsic_gas(self) -> int:
        return intrinsic_gas(self.body["clauses"])

    @property
    def signature(self) -> Optional[bytes]:
        return self._signature

    @signature.setter
    def signature(self, sig: Optional[_AnyBytes]) -> None:
        self._signature = bytes(sig) if sig is not None else sig

    @property
    def origin(self) -> Optional[str]:
        if not self._signature_valid():
            return None

        sig = self.signature
        assert sig is not None

        try:
            my_sign_hash = self.get_signing_hash()
            pub_key = secp256k1.recover(my_sign_hash, sig[:65])
            return "0x" + address.public_key_to_address(pub_key).hex()
        except ValueError:
            return None

    @property
    def delegator(self) -> Optional[str]:
        if not self.is_delegated:
            return None

        if not self._signature_valid():
            return None

        sig = self.signature
        assert sig is not None

        origin = self.origin
        if not origin:
            return None

        try:
            my_sign_hash = self.get_signing_hash(origin)
            pub_key = secp256k1.recover(my_sign_hash, sig[65:])
            return "0x" + address.public_key_to_address(pub_key).hex()
        except ValueError:
            return None

    @property
    def is_delegated(self) -> bool:
        """Check if this transaction is delegated."""
        if not self.body.get("reserved", {}).get("features"):
            return False

        return (
            self.body["reserved"]["features"] & self.DELEGATED_MASK
            == self.DELEGATED_MASK
        )

    @property
    def id(self) -> Optional[str]:  # noqa: A003
        if not self._signature_valid():
            return None

        sig = self.signature
        assert sig is not None

        try:
            my_sign_hash = self.get_signing_hash()
            pub_key = secp256k1.recover(my_sign_hash, sig[:65])
            origin = address.public_key_to_address(pub_key)
            return "0x" + blake2b256([my_sign_hash, origin])[0].hex()
        except ValueError:
            return None

    def _signature_valid(self) -> bool:
        if not self.signature:
            return False
        else:
            expected_sig_len = 65 * 2 if self.is_delegated else 65
            return len(self.signature) == expected_sig_len

    def encode(self, force_unsigned: bool = False) -> bytes:
        """Encode the tx into bytes"""
        reserved_list = self._encode_reserved()
        temp = deepcopy(self.body)
        temp["reserved"] = reserved_list

        if not self.signature or force_unsigned:
            return ComplexCodec(UnsignedTxWrapper).encode(temp)
        else:
            temp.update({"signature": self.signature})
            return ComplexCodec(SignedTxWrapper).encode(temp)

    @staticmethod
    def decode(raw: _AnyBytes, unsigned: bool) -> "Transaction":
        """Return a Transaction type instance"""
        sig = None

        if unsigned:
            body = ComplexCodec(UnsignedTxWrapper).decode(raw)
        else:
            body = ComplexCodec(SignedTxWrapper).decode(raw)
            sig = body.pop("signature")  # bytes

        r = body.pop("reserved", [])  # list of bytes
        if r:
            if not r[-1]:
                raise BadTransaction("invalid reserved fields: not trimmed.")

            reserved = {"features": FeaturesKind.deserialize(r[0])}
            if len(r) > 1:
                reserved["unused"] = r[1:]
            body["reserved"] = RESERVED(reserved)

        # Now body is a "dict", we try to check if it is in good shape.

        # Check if clause is in good shape.
        body["clauses"] = [CLAUSE(c) for c in body["clauses"]]

        tx = Transaction(body)

        if sig:
            tx.signature = sig

        return tx

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Transaction):
            return NotImplemented

        return (
            self.signature == other.signature
            # only because of ["reserved"]["unused"] may glitch.
            and self.encode() == other.encode()
        )

    @deprecated_to_property
    def get_delegator(self) -> Optional[str]:
        return self.delegator

    @deprecated_to_property
    def get_intrinsic_gas(self) -> int:
        """Get the rough gas this tx will consume"""
        return self.intrinsic_gas

    @deprecated_to_property
    def get_signature(self) -> Optional[bytes]:
        """Get the signature of current transaction."""
        return self.signature

    @deprecated_to_property
    def set_signature(self, sig: _AnyBytes) -> None:
        """Set the signature"""
        self.signature = sig

    @deprecated_to_property
    def get_origin(self) -> Optional[str]:
        return self.origin

    @deprecated_to_property
    def get_id(self) -> Optional[str]:
        return self.id
