# This file is Copyright 2020 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#
import logging
from volatility.framework import interfaces, renderers
from volatility.framework.configuration import requirements
from volatility.framework.renderers import format_hints
from volatility.framework.layers import intel
from volatility.plugins.windows.registry import hivelist
from volatility.plugins.windows import hashdump, poolscanner
from Crypto.Hash import MD5, SHA256
from Crypto.Cipher import ARC4, DES, AES
from struct import unpack, pack
import collections

vollog = logging.getLogger(__name__)

class Lsadump(interfaces.plugins.PluginInterface):
    """Dumps lsa secrets from memory"""
    @classmethod
    def get_requirements(cls):
        return [requirements.TranslationLayerRequirement(name = 'primary',
                                                     description = 'Memory layer for the kernel',
                                                     architectures = ["Intel32", "Intel64"]),
            requirements.SymbolTableRequirement(name = "nt_symbols",
                                                description = "Windows kernel symbols"),
            requirements.PluginRequirement(name = 'hivelist', plugin = hivelist.HiveList, version = (1, 0, 0))#,
            ]
    
    @classmethod
    def decrypt_aes(cls, secret, key):
        """
        Based on code from http://lab.mediaservice.net/code/cachedump.rb
        """
        sha = SHA256.new()
        sha.update(key)
        for _i in range(1, 1000 + 1):
            sha.update(secret[28:60])
        aeskey = sha.digest()

        data = b""
        for i in range(60, len(secret), 16):
            aes = AES.new(aeskey, AES.MODE_CBC, '\x00' * 16)
            buf = secret[i : i + 16]
            if len(buf) < 16:
                buf += (16 - len(buf)) * "\00"
            data += aes.decrypt(buf)

        return data
    
    @classmethod
    def get_lsa_key(cls, sechive, bootkey, vista_or_later):
        if not bootkey:
            return None

        root = sechive.root_cell_offset
        if not root:
            return None

        
        
        if vista_or_later:
            policy_key = 'PolEKList'
        else:
            policy_key = 'PolSecretEncryptionKey'

        enc_reg_key = sechive.get_key("Policy\\"+policy_key)                    
        if not enc_reg_key:
            return None
        enc_reg_value = next(enc_reg_key.get_values())


        if not enc_reg_value:
            return None
        
        obf_lsa_key = sechive.read(enc_reg_value.Data+4, enc_reg_value.DataLength)

        if not obf_lsa_key:
            return None
        if not vista_or_later:
            md5 = MD5.new()
            md5.update(bootkey.encode('latin1'))
            for _i in range(1000):
                md5.update(obf_lsa_key[60:76])
            rc4key = md5.digest()

            rc4 = ARC4.new(rc4key)
            lsa_key = rc4.decrypt(obf_lsa_key[12:60])
            lsa_key = lsa_key[0x10:0x20]
        else:
            lsa_key = cls.decrypt_aes(obf_lsa_key, bootkey.encode('latin1'))
            lsa_key = lsa_key[68:100]
        return lsa_key
    
    @classmethod
    def get_secret_by_name(cls, sechive, name, lsakey, is_vista_or_later):
        try:
            enc_secret_key = sechive.get_key("Policy\\Secrets\\" + name + "\\CurrVal")
        except KeyError:
            vollog.debug("Unable to read cache from memory")
            exit()

        enc_secret_value = next(enc_secret_key.get_values())
        if not enc_secret_value:
            return None

        enc_secret = sechive.read(enc_secret_value.Data+4,
                enc_secret_value.DataLength)
        if not enc_secret:
            return None

        if not is_vista_or_later:
            secret = cls.decrypt_secret(enc_secret[0xC:], lsakey)
        else:
            secret = cls.decrypt_aes(enc_secret, lsakey).decode('latin1')
        return secret

    @classmethod
    def decrypt_secret(cls, secret, key):
        """Python implementation of SystemFunction005.

        Decrypts a block of data with DES using given key.
        Note that key can be longer than 7 bytes."""
        decrypted_data = ''
        j = 0   # key index

        for i in range(0, len(secret), 8):
            enc_block = secret[i:i + 8].decode('latin1')
            block_key = key[j:j + 7]
            des_key = hashdump.Hashdump.str_to_key(block_key.decode('latin1'))
            des = DES.new(des_key.encode('latin1'), DES.MODE_ECB)
            enc_block = enc_block + "\x00" * int(abs(8 - len(enc_block)) % 8)
            decrypted_data += des.decrypt(enc_block.encode('latin1')).decode('latin1')
            j += 7
            if len(key[j:j + 7]) < 7:
                j = len(key[j:j + 7])

        (dec_data_len,) = unpack("<L", decrypted_data[:4].encode('latin1'))

        return decrypted_data[8:8 + dec_data_len]

    def _generator(self, syshive, sechive):

        is_vista_or_later = poolscanner.os_distinguisher(version_check = lambda x: x >= (6, 0),
                                                     fallback_checks = [("KdCopyDataBlock", None, True)])
        vista_or_later = is_vista_or_later(context = self.context, symbol_table = self.config['nt_symbols'])

        bootkey = hashdump.Hashdump.get_bootkey(syshive)
        lsakey = self.get_lsa_key(sechive, bootkey, vista_or_later)
        if not bootkey or not lsakey:
            return None

        secrets_key = sechive.get_key('Policy\\Secrets')
        if not secrets_key:
            return None

        for key in secrets_key.get_subkeys():

            sec_val_key=sechive.get_key('Policy\\Secrets\\' + key.get_key_path().split('\\')[3] + '\\CurrVal')
            if not sec_val_key:
                continue

            enc_secret_value = next(sec_val_key.get_values())
            if not enc_secret_value:
                continue

            enc_secret = sechive.read(enc_secret_value.Data + 4,
                    enc_secret_value.DataLength)
            if not enc_secret:
                continue
            if not vista_or_later:
                secret = self.decrypt_secret(enc_secret[0xC:], lsakey)
            else:
                secret = self.decrypt_aes(enc_secret, lsakey).decode('latin1')


            yield (0,(key.get_name()+'\n', secret+'\n', secret.encode('latin1')))



    def run(self):

        offset = self.config.get('offset', None)


        for hive in hivelist.HiveList.list_hives(self.context,
                                            self.config_path,
                                            self.config['primary'],
                                            self.config['nt_symbols'],
                                            hive_offsets = None if offset is None else [offset]):

            if hive.get_name().split('\\')[-1].upper() == 'SYSTEM':
                syshive=hive
            if hive.get_name().split('\\')[-1].upper() == 'SECURITY':
                sechive=hive  

        return renderers.TreeGrid([("Key", str), ("Secret", str), ('Hex', bytes)], 
                                    self._generator(syshive, sechive))



    