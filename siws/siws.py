import base58

from datetime import datetime
from hashlib import sha256
from typing import Optional, Union
from typing_extensions import Annotated

from ecdsa import SECP256k1, VerifyingKey
from pydantic import AnyUrl, BaseModel, Field, TypeAdapter, NonNegativeInt, BeforeValidator

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from siws import exceptions
from siws.custom_types import CustomDateTime
from siws.parsed import ABNFParsedMessage, RegExpParsedMessage
from siws.utils import build_signature, encode_defunc


# NOTE: Do not override the original uri string, just do validation
# https://github.com/pydantic/pydantic/issues/7186#issuecomment-1874338146
AnyUrlTypeAdapter = TypeAdapter(AnyUrl)
AnyUrlStr = Annotated[
    str,
    BeforeValidator(lambda value: AnyUrlTypeAdapter.validate_python(value) and value),
]


class SiwsMessage(BaseModel):
    """
    A class meant to fully encompass a Sign-in with Stacks message.
    Its utility striclty remains within formatting and compliance.
    """

    domain: str = Field(pattern="^[^/?#]+$")
    address: str
    uri: AnyUrlStr
    version: str
    chain_id: NonNegativeInt
    issued_at: CustomDateTime
    nonce: str = Field(min_length=8)
    statement: Optional[str] = Field(None, pattern="^[^\n]+$")
    expiration_time: Optional[CustomDateTime] = Field(None)
    not_before: Optional[CustomDateTime] = Field(None)
    request_id: Optional[str] = Field(None)
    resources: list[AnyUrl] = Field(None, min_length=1)

    def __init__(self, message: Union[str, dict], abnf: bool = True):
        if isinstance(message, str):
            if abnf:
                parsed_message = ABNFParsedMessage(message=message)
            else:
                parsed_message = RegExpParsedMessage(message=message)
            message_dict = parsed_message.__dict__
        elif isinstance(message, dict):
            message_dict = message
        else:
            raise TypeError
        # There is some redundancy in the checks when deserialising a message.
        super().__init__(**message_dict)

    def prepare_message(self) -> str:
        """
        Retrieve an EIP-4361 formatted message for signature.
        It is recommended to instead use sign_message() which will resolve
        to the correct method based on the [type] attribute
        of this object, in case of other formats being implemented.
        :return: EIP-4361 formatted message, ready for EIP-191 signing.
        """
        header = (
            f"{self.domain} wants you to sign in with your Solana account:"
        )

        uri_field = f"URI: {self.uri}"

        prefix = "\n".join([header, self.address])

        version_field = f"Version: {self.version}"

        chain_field = f"Chain ID: {self.chain_id or 1}"

        nonce_field = f"Nonce: {self.nonce}"

        suffix_array = [uri_field, version_field, chain_field, nonce_field]

        if self.issued_at is None:
            self.issued_at = datetime.utcnow().isoformat()

        suffix_array.append(f"Issued At: {self.issued_at}")

        if self.expiration_time:
            suffix_array.append(f"Expiration Time: {self.expiration_time}")

        if self.not_before:
            suffix_array.append(f"Not Before: {self.not_before}")

        if self.request_id:
            suffix_array.append(f"Request ID: {self.request_id}")

        if self.resources:
            suffix_array.append("\n".join(
                ["Resources:"] +
                [f"- {resource}" for resource in self.resources]
            ))

        suffix = "\n".join(suffix_array)

        if self.statement:
            prefix = "\n\n".join([prefix, self.statement])
        else:
            prefix += "\n"

        return "\n\n".join([prefix, suffix])

    def verify(
        self,
        signature: str,
        *,
        domain: Optional[str] = None,
        nonce: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:

        message = self.prepare_message().encode()

        verification_time = datetime.utcnow() if not timestamp else timestamp
        if domain and domain != self.domain:
            raise exceptions.DomainMismatch
        elif nonce and self.nonce != nonce:
            raise exceptions.NonceMismatch
        elif (
            self.expiration_time and verification_time >= self.expiration_time
        ):
            raise exceptions.ExpiredMessage
        elif self.not_before and verification_time <= self.not_before.date:
            raise exceptions.NotYetValidMessage

        try:
            verify_key = VerifyKey(base58.b58decode(self.address))
            signature_bytes = base58.b58decode(signature)
            verify_key.verify(message, signature_bytes)
        except BadSignatureError as e:
            raise exceptions.InvalidSignature from e
