# The MIT License (MIT)

# Copyright (c) 2021-2024 Krux contributors

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
# pylint: disable=W0102
import time

try:
    import urandom as random
except:
    import random
from binascii import hexlify
from hashlib import sha256
from embit import bip32, bip39
from embit.wordlists.bip39 import WORDLIST
from embit.networks import NETWORKS
from .settings import TEST_TXT

DER_SINGLE = "m/%dh/%dh/%dh"
DER_MULTI = "m/%dh/%dh/%dh/2h"
HARDENED_STR_REPLACE = "'"

SINGLESIG_SCRIPT_PURPOSE = {
    "p2pkh": 44,
    "p2sh-p2wpkh": 49,
    "p2wpkh": 84,
    "p2tr": 86,
}

MULTISIG_SCRIPT_PURPOSE = 48


class Key:
    """Represents a BIP-39 mnemonic-based private key"""

    def __init__(
        self,
        mnemonic,
        multisig,
        network=NETWORKS[TEST_TXT],
        passphrase="",
        account_index=0,
        script_type="p2wpkh",
    ):
        self.mnemonic = mnemonic
        self.multisig = multisig
        self.network = network
        self.passphrase = passphrase
        self.account_index = account_index
        self.script_type = script_type if not multisig else "p2wsh"
        self.root = bip32.HDKey.from_seed(
            bip39.mnemonic_to_seed(mnemonic, passphrase), version=network["xprv"]
        )
        self.fingerprint = self.root.child(0).fingerprint
        self.derivation = self.get_default_derivation(
            self.multisig, self.network, self.account_index, self.script_type
        )
        self.account = self.root.derive(self.derivation).to_public()

    def xpub(self, version=None):
        """Returns the xpub representation of the extended master public key"""
        return self.account.to_base58(version)

    def get_xpub(self, path):
        """Returns the xpub for the provided path"""
        return self.root.derive(path).to_public()

    def key_expression(self, version=None):
        """Returns the extended master public key (xpub/ypub/zpub) in key expression format
        per https://github.com/bitcoin/bips/blob/master/bip-0380.mediawiki#key-expressions,
        prefixed with fingerprint and derivation.
        """
        return "[%s%s]%s" % (
            self.fingerprint_hex_str(False),
            self.derivation[
                1:
            ],  # remove leading m, necessary for creating a descriptor
            self.account_pubkey_str(version),
        )

    def account_pubkey_str(self, version=None):
        """Returns the account extended public key (xpub/ypub/zpub)"""
        return self.account.to_base58(version)

    def fingerprint_hex_str(self, pretty=False):
        """Returns the master key fingerprint in hex format"""
        formatted_txt = "⊚ %s" if pretty else "%s"
        return formatted_txt % hexlify(self.fingerprint).decode("utf-8")

    def derivation_str(self, pretty=False):
        """Returns the derivation path for the Hierarchical Deterministic Wallet to
        be displayed as string
        """
        formatted_txt = "↳ %s" if pretty else "%s"
        return (formatted_txt % self.derivation).replace("h", HARDENED_STR_REPLACE)

    def sign(self, message_hash):
        """Signs a message with the extended master private key"""
        return self.root.derive(self.derivation).sign(message_hash)

    def sign_at(self, derivation, message_hash):
        """Signs a message at an adress derived from master key (code adapted from specterDIY)"""
        from embit import ec
        from embit.util import secp256k1

        prv = self.root.derive(derivation).key
        sig = secp256k1.ecdsa_sign_recoverable(
            message_hash, prv._secret  # pylint: disable=W0212
        )
        flag = sig[64]
        flag = bytes([27 + flag + 4])
        ec_signature = ec.Signature(sig[:64])
        ser = flag + secp256k1.ecdsa_signature_serialize_compact(
            ec_signature._sig  # pylint: disable=W0212
        )
        return ser

    @staticmethod
    def pick_final_word(entropy, words):
        """Returns a random final word with a valid checksum for the given list of
        either 11 or 23 words
        """
        if len(words) != 11 and len(words) != 23:
            raise ValueError("must provide 11 or 23 words")

        random.seed(int(time.ticks_ms() + entropy))
        return random.choice(Key.get_final_word_candidates(words))

    @staticmethod
    def get_default_derivation(multisig, network, account=0, script_type="p2wpkh"):
        """Return the Krux default derivation path for single-sig or multisig"""
        der_format = DER_MULTI if multisig else DER_SINGLE
        purpose = (
            MULTISIG_SCRIPT_PURPOSE
            if multisig
            else SINGLESIG_SCRIPT_PURPOSE[script_type]
        )
        return der_format % (purpose, network["bip32"], account)

    @staticmethod
    def format_derivation(derivation, pretty=False):
        """Helper method to display the derivation path formatted"""
        formatted_txt = "↳ %s" if pretty else "%s"
        return (formatted_txt % derivation).replace("h", HARDENED_STR_REPLACE)

    @staticmethod
    def format_fingerprint(fingerprint, pretty=False):
        """Helper method to display the fingerprint formatted"""
        formatted_txt = "⊚ %s" if pretty else "%s"
        return formatted_txt % hexlify(fingerprint).decode("utf-8")

    @staticmethod
    def get_final_word_candidates(words):
        """Returns a list of valid final words"""
        if len(words) != 11 and len(words) != 23:
            raise ValueError("must provide 11 or 23 words")

        accu = 0
        for index in [WORDLIST.index(x) for x in words]:
            accu = (accu << 11) + index

        # in bits: final entropy, needed entropy, checksum
        len_target = (len(words) * 11 + 11) // 33 * 32
        len_needed = len_target - (len(words) * 11)
        len_cksum = len_target // 32

        candidates = []
        for i in range(2**len_needed):
            entropy = (accu << len_needed) + i
            ck_bytes = sha256(entropy.to_bytes(len_target // 8, "big")).digest()
            cksum = int.from_bytes(ck_bytes, "big") >> 256 - len_cksum
            last_word = WORDLIST[(i << len_cksum) + cksum]
            candidates.append(last_word)

        return candidates
