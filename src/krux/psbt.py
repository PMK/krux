# The MIT License (MIT)

# Copyright (c) 2021-2022 Krux contributors

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
from embit import script
from embit.psbt import DerivationPath, PSBT
from embit.finalizer import parse_multisig
from ur.ur import UR
import urtypes
from urtypes.crypto import CRYPTO_PSBT
from .baseconv import base_encode, base_decode
from .format import satcomma
from .krux_settings import t
from .qr import FORMAT_PMOFN


class PSBTSigner:
    """Responsible for validating and signing PSBTs"""

    def __init__(self, wallet, psbt_data, qr_format):
        self.wallet = wallet
        self.base_encoding = None
        self.ur_type = None
        self.qr_format = qr_format
        # Parse the PSBT
        if isinstance(psbt_data, UR):
            try:
                self.psbt = PSBT.parse(
                    urtypes.crypto.PSBT.from_cbor(psbt_data.cbor).data
                )
                self.ur_type = CRYPTO_PSBT
            except:
                raise ValueError("invalid PSBT")
        else:
            # Process as bytes
            psbt_data = psbt_data.encode() if isinstance(psbt_data, str) else psbt_data
            try:
                self.psbt = PSBT.parse(psbt_data)
                if self.qr_format == FORMAT_PMOFN:
                    # We can't return the PSBT as a multi-part sequence of bytes, so convert to
                    # base64 first
                    self.base_encoding = 64
            except:
                try:
                    self.psbt = PSBT.parse(base_decode(psbt_data, 64))
                    self.base_encoding = 64
                except:
                    try:
                        self.psbt = PSBT.parse(base_decode(psbt_data, 58))
                        self.base_encoding = 58
                    except:
                        try:
                            self.psbt = PSBT.parse(base_decode(psbt_data, 43))
                            self.base_encoding = 43
                        except:
                            raise ValueError("invalid PSBT")
        # Validate the PSBT
        # From: https://github.com/diybitcoinhardware/embit/blob/master/examples/change.py#L110
        xpubs = self.xpubs()
        self.policy = None
        for inp in self.psbt.inputs:
            # get policy of the input
            try:
                inp_policy = get_policy(inp, inp.witness_utxo.script_pubkey, xpubs)
            except:
                raise ValueError("Unable to get policy")
            # if policy is None - assign current
            if self.policy is None:
                self.policy = inp_policy
            # otherwise check that everything in the policy is the same
            else:
                # check policy is the same
                if self.policy != inp_policy:
                    raise ValueError("mixed inputs in the tx")

        if is_multisig(self.policy) and not self.wallet.is_multisig():
            raise ValueError("multisig tx")
        if not is_multisig(self.policy) and self.wallet.is_multisig():
            raise ValueError("not multisig tx")

        # If a wallet descriptor has been loaded, verify that the wallet
        # policy matches the PSBT policy
        if self.wallet.is_loaded():
            if self.wallet.policy != self.policy:
                raise ValueError("policy mismatch")

    def outputs(self):
        """Returns a list of messages describing where amounts are going"""
        inp_amount = 0
        for inp in self.psbt.inputs:
            inp_amount += inp.witness_utxo.value
        resume_inputs_str = (
            (t("Inputs (%d): ") % len(self.psbt.inputs))
            + (t("₿%s") % satcomma(inp_amount))
            + "\n\n"
        )

        self_transfer_list = []
        change_list = []
        spend_list = []

        self_amount = 0
        change_amount = 0
        spend_amount = 0
        resume_spend_str = ""
        resume_self_or_change_str = ""

        xpubs = self.xpubs()
        for i, out in enumerate(self.psbt.outputs):
            out_policy = get_policy(out, self.psbt.tx.vout[i].script_pubkey, xpubs)
            # if policy is the same - probably change
            if out_policy == self.policy:
                # double-check that it's change
                # we already checked in get_cosigners and parse_multisig
                # that pubkeys are generated from cosigners,
                # and witness script is corresponding multisig
                # so we only need to check that scriptpubkey is generated from
                # witness script

                # empty script by default
                sc = script.Script(b"")
                # multisig, we know witness script
                if self.policy["type"] == "p2wsh":
                    sc = script.p2wsh(out.witness_script)
                elif self.policy["type"] == "p2sh-p2wsh":
                    sc = script.p2sh(script.p2wsh(out.witness_script))
                # single-sig
                elif "pkh" in self.policy["type"]:
                    if len(list(out.bip32_derivations.values())) > 0:
                        der = list(out.bip32_derivations.values())[0].derivation
                        my_hd_prvkey = self.wallet.key.root.derive(der)
                        if self.policy["type"] == "p2wpkh":
                            sc = script.p2wpkh(my_hd_prvkey)
                        elif self.policy["type"] == "p2sh-p2wpkh":
                            sc = script.p2sh(script.p2wpkh(my_hd_prvkey))

            # Address is from my wallet
            if sc.data == self.psbt.tx.vout[i].script_pubkey.data:
                # is addr_type change?
                if (
                    len(list(out.bip32_derivations.values())) > 0
                    and list(out.bip32_derivations.values())[0].derivation[3] == 1
                ):
                    change_list.append(
                        (
                            self.psbt.tx.vout[i].script_pubkey.address(
                                network=self.wallet.key.network
                            ),
                            self.psbt.tx.vout[i].value,
                        )
                    )
                    change_amount += self.psbt.tx.vout[i].value
                else:
                    self_transfer_list.append(
                        (
                            self.psbt.tx.vout[i].script_pubkey.address(
                                network=self.wallet.key.network
                            ),
                            self.psbt.tx.vout[i].value,
                        )
                    )
                    self_amount += self.psbt.tx.vout[i].value
            else:  # Address is from other wallet
                spend_list.append(
                    (
                        self.psbt.tx.vout[i].script_pubkey.address(
                            network=self.wallet.key.network
                        ),
                        self.psbt.tx.vout[i].value,
                    )
                )
                spend_amount += self.psbt.tx.vout[i].value

        if len(spend_list) > 0:
            resume_spend_str = (
                (t("Spend (%d): ") % len(spend_list))
                + (t("₿%s") % satcomma(spend_amount))
                + "\n\n"
            )

        if len(self_transfer_list) + len(change_list) > 0:
            resume_self_or_change_str = (
                (
                    t("Self-transfer or Change (%d): ")
                    % (len(self_transfer_list) + len(change_list))
                )
                + (t("₿%s") % satcomma(self_amount + change_amount))
                + "\n\n"
            )

        fee = inp_amount - spend_amount - self_amount - change_amount
        resume_fee_str = t("Fee: ") + (t("₿%s") % satcomma(fee))

        messages = []
        # first screen - resume
        messages.append(
            resume_inputs_str
            + resume_spend_str
            + resume_self_or_change_str
            + resume_fee_str
        )

        # sequence of spend
        for i, out in enumerate(spend_list):
            messages.append(
                (t("%d. Spend: \n\n%s\n\n") % (i + 1, out[0]))
                + (t("₿%s") % satcomma(out[1]))
            )

        # sequence of self_transfer
        for i, out in enumerate(self_transfer_list):
            messages.append(
                (t("%d. Self-transfer: \n\n%s\n\n") % (i + 1, out[0]))
                + (t("₿%s") % satcomma(out[1]))
            )

        # sequence of change
        for i, out in enumerate(change_list):
            messages.append(
                (t("%d. Change: \n\n%s\n\n") % (i + 1, out[0]))
                + (t("₿%s") % satcomma(out[1]))
            )

        return messages

    def sign(self):
        """Signs the PSBT"""
        sigs_added = self.psbt.sign_with(self.wallet.key.root)
        if sigs_added == 0:
            raise ValueError("cannot sign")

        trimmed_psbt = PSBT(self.psbt.tx)
        for i, inp in enumerate(self.psbt.inputs):
            trimmed_psbt.inputs[i].partial_sigs = inp.partial_sigs

        self.psbt = trimmed_psbt

    def psbt_qr(self):
        """Returns the psbt in the same form it was read as a QR code"""
        psbt_data = self.psbt.serialize()
        if self.base_encoding is not None:
            psbt_data = base_encode(psbt_data, self.base_encoding).decode()

        if self.ur_type == CRYPTO_PSBT:
            return (
                UR(
                    CRYPTO_PSBT.type,
                    urtypes.crypto.PSBT(psbt_data).to_cbor(),
                ),
                self.qr_format,
            )
        return psbt_data, self.qr_format

    def xpubs(self):
        """Returns the xpubs in the PSBT mapped to their derivations, falling back to
        the wallet descriptor xpubs if not found
        """
        if self.psbt.xpubs:
            return self.psbt.xpubs

        if not self.wallet.descriptor:
            raise ValueError("missing xpubs")

        descriptor_keys = (
            [self.wallet.descriptor.key]
            if self.wallet.descriptor.key
            else self.wallet.descriptor.keys
        )
        xpubs = {}
        for descriptor_key in descriptor_keys:
            if descriptor_key.origin:
                # Pure xpub descriptors (Blue Wallet) don't have origin data
                xpubs[descriptor_key.key] = DerivationPath(
                    descriptor_key.origin.fingerprint, descriptor_key.origin.derivation
                )
        return xpubs


def is_multisig(policy):
    """Returns a boolean indicating if the policy is a multisig"""
    return (
        "type" in policy
        and "p2wsh" in policy["type"]
        and "m" in policy
        and "n" in policy
        and "cosigners" in policy
    )


# From: https://github.com/diybitcoinhardware/embit/blob/master/examples/change.py#L41
def get_cosigners(pubkeys, derivations, xpubs):
    """Returns xpubs used to derive pubkeys using global xpub field from psbt"""
    cosigners = []
    for _, pubkey in enumerate(pubkeys):
        if pubkey not in derivations:
            raise ValueError("missing derivation")
        der = derivations[pubkey]
        for xpub in xpubs:
            origin_der = xpubs[xpub]
            # check fingerprint
            if origin_der.fingerprint == der.fingerprint:
                # check derivation - last two indexes give pub from xpub
                if origin_der.derivation == der.derivation[:-2]:
                    # check that it derives to pubkey actually
                    if xpub.derive(der.derivation[-2:]).key == pubkey:
                        # append strings so they can be sorted and compared
                        cosigners.append(xpub.to_base58())
                        break
    if len(cosigners) != len(pubkeys):
        raise ValueError("cannot get all cosigners")
    return sorted(cosigners)


# From: https://github.com/diybitcoinhardware/embit/blob/master/examples/change.py#L64
def get_policy(scope, scriptpubkey, xpubs):
    """Parse scope and get policy"""
    # we don't know the policy yet, let's parse it
    script_type = scriptpubkey.script_type()
    # p2sh can be either legacy multisig, or nested segwit multisig
    # or nested segwit singlesig
    if script_type == "p2sh":
        if scope.witness_script is not None:
            script_type = "p2sh-p2wsh"
        elif (
            scope.redeem_script is not None
            and scope.redeem_script.script_type() == "p2wpkh"
        ):
            script_type = "p2sh-p2wpkh"
    policy = {"type": script_type}
    # expected multisig
    if "p2wsh" in script_type and scope.witness_script is not None:
        m, pubkeys = parse_multisig(scope.witness_script)
        # check pubkeys are derived from cosigners
        cosigners = get_cosigners(pubkeys, scope.bip32_derivations, xpubs)
        policy.update({"m": m, "n": len(cosigners), "cosigners": cosigners})
    return policy
