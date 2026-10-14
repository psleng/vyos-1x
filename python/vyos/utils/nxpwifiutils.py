'''
NXP Wifi related utilities.
'''

import os
import subprocess
import re


# Build-time marker that this image provisions per-radio wifi MACs from nvmem.
# build-vyos-image writes it from the flavor pinmap WIFI_MAC_NVMEM_SOURCE /
# WIFI_INTERFACES; it is absent on Perle builds without an identity EEPROM
# (e.g. the AM64x EVM) -- the signal to leave the module's own MAC alone.
WIFI_INTERFACES_CONF = '/usr/lib/igos/wifi-interfaces.conf'


def pcie_wifi_nxp_model() -> str:
    '''
    Returns NXP wifi PCIE model if one is installed, else ""

    This corresponds to the base section value in the config file
    /lib/firmware/nxp/wifi_mod_para.conf
    '''
    nxpmodel = ''
    if not os.path.exists('/sys/module/moal'):
        return nxpmodel  # Not using NXP at all

    # Get NXP wifi devices on PCI
    ret = subprocess.run(['lspci', '-vmm'], stdout=subprocess.PIPE)
    for i in ret.stdout.decode().split('\n'):
        if not i:
            continue
        tag, val = i.split(sep='\t', maxsplit=1)
        if tag != 'Device:':
            continue
        if 'NXP' not in val or 'Wi-Fi' not in val:
            continue

        # NXP wifi device.  Get the 4-digit model.
        m = re.match(r'.* [0-9][0-9][A-Z]([0-9]{4}).*', val)
        if m:
            try:
                nxpmodel = 'PCIE' + m.groups()[0]
                break
            except Exception:
                pass

    return nxpmodel


class NxpConf:
    '''
    NXP /lib/firmware/nxp/*.conf parser.

    The format is just:

    section = {
        foo = 42
        bar = baz
    }
    '''
    def __init__(self, path: str):
        self.path = path

    def parse(self) -> dict[str, dict]:
        '''Do the parse'''
        sectdict: dict[str, dict] = {}
        lineno = 0
        cursect: dict[str, str] = {}
        sectname = ''
        with open(self.path) as fp:
            for i in fp.readlines():
                lineno += 1
                i = i.strip()
                if not i or i.startswith('#'):
                    continue

                if not sectname:
                    # Section start "foo = {" expected
                    try:
                        sectname, _, brace = i.split(maxsplit=2)
                        if not brace.startswith('{'):
                            raise ValueError('Opening brace expected')
                    except ValueError:
                        raise ValueError('Expected "name = {" at line '
                                         f'{lineno}: {i}') from None
                    cursect = {}
                elif i == '}':
                    # End section
                    sectdict[sectname] = cursect
                    sectname, cursect = '', {}
                else:
                    # Inside a section
                    try:
                        name, val = i.split('=', 1)
                    except ValueError:
                        raise ValueError(f'Expected "name = val" at line '
                                         f'{lineno} in section {sectname}: '
                                         f'{i}') from None
                    cursect[name] = val

        return sectdict


def getphymac(wifi: dict) -> str:
    '''
    PSL: Get preferred MAC for phy if none provided by VyOS config.

    MACs cannot be programmed into some NXP devices.
    Rather than falling back to the default hardware
    /sys/class/ieee80211/*/addresses as VyOS normally does,
    use either any supplied module configuration value from
    /lib/firmware/nxp/wifi_mod_para.conf or else fall back to
    the existing interface address.

    For module configuration we are looking for config lines of the form:

    PCIE9098_0 = {
        ...
        mac_addr=00:40:02:01:02:03
        ...
    }

    ...for both PCIE9098_0 and PCIE9098_1 sections or however many phys
    the device has, in this case for a PCIE9098 card, but it can be
    any NXP model.

    NOTE: This function returns '' if we are not equipped with an
    NXP like device. VyOS will use the hardware address as usual.

    NOTE: generate() below masks off the lower 4 bits and rebuilds them,
    so the results will look a bit different than you might expect.
    '''

    hwmac = ''
    if 'mac' in wifi:
        # VyOS defined MAC already supplied
        return hwmac

    nxp_model = pcie_wifi_nxp_model()
    if not nxp_model:
        # Not NXP hardware so nothing to do
        return hwmac

    # Try NXP config file mac_addr
    try:
        nxpconf = NxpConf('/lib/firmware/nxp/wifi_mod_para.conf')
        nxpconfd = nxpconf.parse()
        phynum = wifi.get('physical_device', '0')[-1]
        ifc = f'{nxp_model}_{phynum}'
        hwmac = nxpconfd[ifc]['mac_addr']
    except Exception:
        pass

    if not hwmac:
        # Get mac from existing iface value instead
        import subprocess
        try:
            ret = subprocess.run(['ip', '-json', '-detail', 'link',
                                  'list', 'dev', wifi['ifname']],
                                 stdout=subprocess.PIPE)
            import json
            j = json.loads(ret.stdout.decode().strip())
            hwmac = j[0]['address']
        except Exception:
            pass

    return hwmac


def provisioned_wifi_mac(ifname: str) -> str:
    '''
    PSL: Return the EXACT factory MAC provisioned for a wifi radio netdev.

    iGOS boards store a per-radio MAC in the board EEPROM (nvmem). The build
    writes /usr/lib/igos/wifi-interfaces.conf from the flavor pinmap
    WIFI_INTERFACES, mapping each radio netdev to the nvmem cell holding its
    MAC, plus a "source <dev>" line naming the perle-device-info platform
    device that exposes those cells as raw-byte sysfs attributes.

    This reads the cell for ``ifname`` and returns it as a colon-hex MAC, to be
    applied VERBATIM (unlike getphymac's result, which the caller LAA-mangles).
    Returns '' when there is no mapping, no hardware, or anything is unreadable,
    so the caller falls back to the derived address.
    '''
    conf = WIFI_INTERFACES_CONF
    source = ''
    cell = ''
    try:
        with open(conf) as fp:
            for line in fp:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                if parts[0] == 'source':
                    source = parts[1]
                elif parts[0] == ifname:
                    cell = parts[1]
    except OSError:
        return ''

    if not source or not cell:
        return ''

    try:
        with open(f'/sys/devices/platform/{source}/{cell}', 'rb') as fp:
            raw = fp.read(6)
    except OSError:
        return ''

    if len(raw) != 6:
        return ''

    # Reject a blank/unprogrammed cell (all 0x00 or all 0xff) or a multicast
    # address (odd first octet) -- fall back to the derived MAC instead of
    # applying a bogus one.
    if raw == b'\x00' * 6 or raw == b'\xff' * 6 or (raw[0] & 0x01):
        return ''

    return ':'.join(f'{b:02x}' for b in raw)


def module_assigns_own_mac(wifi: dict) -> bool:
    '''
    PSL: True when VyOS must leave an NXP radio's MAC alone so the moal module's
    self-assigned address stands.

    This holds on Perle builds that do NOT provision per-radio MACs from nvmem:
    there is no WIFI_INTERFACES_CONF because the flavor pinmap declared no
    WIFI_MAC_NVMEM_SOURCE / WIFI_INTERFACES (e.g. the AM64x EVM, which has no
    identity EEPROM). generate() then applies the module's existing MAC verbatim
    instead of deriving a locally administered one.

    Returns False for non-NXP hardware (upstream LAA derivation kept) and when
    nvmem provisioning IS configured (the verbatim nvmem MAC, or the derived MAC
    for an unprovisioned radio, is used as before).
    '''
    if 'mac' in wifi:
        return False
    if not pcie_wifi_nxp_model():
        return False
    return not os.path.exists(WIFI_INTERFACES_CONF)


def device_info_string(cell: str) -> str:
    '''
    PSL: Read a string-valued identity EEPROM cell exposed by perle-device-info.

    The build records the perle-device-info platform device in the "source" line
    of WIFI_INTERFACES_CONF (the same file provisioned_wifi_mac() uses). This
    reads /sys/devices/platform/<source>/<cell> and returns its ASCII value with
    any NUL/0xff padding and surrounding whitespace stripped.

    Returns '' when there is no source (e.g. a board without an identity EEPROM,
    such as the AM64x EVM) or the cell is missing/unreadable.
    '''
    source = ''
    try:
        with open(WIFI_INTERFACES_CONF) as fp:
            for line in fp:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[0] == 'source':
                    source = parts[1]
                    break
    except OSError:
        return ''

    if not source:
        return ''

    try:
        with open(f'/sys/devices/platform/{source}/{cell}', 'rb') as fp:
            raw = fp.read()
    except OSError:
        return ''

    # device-info string cells already strip trailing 0x00/0xff; strip again
    # defensively, then drop surrounding whitespace.
    return raw.split(b'\x00', 1)[0].rstrip(b'\xff').decode('ascii', 'ignore').strip()


def provisioned_ap_ssid() -> str:
    '''
    PSL: Factory-default access-point SSID from the identity EEPROM
    (perle-device-info 'ssid' cell).

    Returns '' unless the cell holds a valid 1-32 character 802.11 SSID, so the
    caller leaves the SSID unset (and VyOS enforces its usual requirement) when
    the hardware is unprovisioned.
    '''
    ssid = device_info_string('ssid')
    if 1 <= len(ssid) <= 32:
        return ssid
    return ''


def provisioned_ap_passphrase() -> str:
    '''
    PSL: Factory-default WPA passphrase from the identity EEPROM
    (perle-device-info 'password' cell).

    Returns '' unless the cell holds a valid 8-63 character WPA passphrase, so a
    too-short/blank cell is never injected (which would only trip hostapd or the
    existing passphrase-length check).
    '''
    passphrase = device_info_string('password')
    if 8 <= len(passphrase) <= 63:
        return passphrase
    return ''
