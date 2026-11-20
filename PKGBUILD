# Maintainer: Bill Sideris <bill88t@feline.gr>

pkgname=beryllium-tools
pkgver=1.15.0
pkgrel=1
pkgdesc="A grand collection of tools"

arch=('any')
url="https://github.com/beryllium-org/beryllium-tools"
license=('GPL3')

groups=(beryllium)
depends=('python' 'arch-install-scripts' 'systemd' 'gcc')
provides=('bredos-tools')
replaces=('bredos-tools')
conflicts=('bredos-tools')

optdepends=(
    'dtc: Compile device trees with the dtsc helper'
    'android-tools: Use rv2rk and rvflasher scripts for flashing Ky RISC-V chips'
    'cryptsetup: Convert and manage the LUKS2 container with beryllium-fde'
    'grub: Install and regenerate GRUB for beryllium-fde'
    'dracut: Rebuild initramfs images with beryllium-fde'
    'btrfs-progs: Resize the root filesystem during beryllium-fde conversion'
)

source=('dtsc.py'
        'rkdump.sh'
        'beryllium-chroot.sh'
        'lsmmc.py'
        'wakeupctl.py'
        'grub-password.sh'
        'grub-apply-unrestrict.py'
        'grub-unrestrict.hook'
        'sleepctl.py'
        'sleepctld.sh'
        'sleepctl.service'
        'rv2rk.py'
        'FSBL.bin'
        'u-boot.itb'
        'rvflasher.py'
        'FSBL_FLASH.bin'
        'u-boot_FLASH.itb'
        'partition_universal.json'
        'bootinfo_sd.bin'
        'rkdump.1'
        'grub-password.1'
        'dtsc.1'
        'sleepctl.1'
        'sleepctld.1'
        'wakeupctl.1'
        'beryllium-chroot.8'
        'beryllium-fde.py'
        'beryllium-fde.8')
sha256sums=('b56ef504614e3ab02fbaf1211196953b783ab1ca6b5855a265f8e9417891628e'
            'e7404e6dc96d40d0619daa25287f0e438e9a99484a8b7fa4f8d2adcf078932c1'
            '3f8adbb46b4d0345ad558393ab66b4fa50d33c5761b742a513cf7aff803a94ee'
            '5b8dd87f4141cdce1aa73eb19fca30370c4ab5fc1f97f8077cd01bf68a51a4cc'
            '12047c25a46a9e0def5cc687ed0d1690d8e80853680280d347b1102b5203bde3'
            'be81b089e5bb91a9a3c2ae6c6658d538ea2b031263e3ac9685be2c1ec87fba6f'
            'f430e73417126b2dcf84cfaa02b3fb5c520da5794faf8d29f9c8531ec970614e'
            'ffabbfbfdca391f8616340a4323eddb868040ca35c24bd8d7d6c5df3b2cc77ac'
            '797103948bd377f8aae15527eafcdb7e4d9121d4ce57ac7af0943bae26dbdca5'
            '3c830af040906c5de9e4f82f7089775b585cd46eafc435053bef1aee717f8d49'
            '7bde0bb9eb48c7c560194d04a0864c833e63662d3ff527a23844eaa0d1849101'
            'adbcefef20db5743a34b75f463c73aefcfb86b16badc33f0edb30b947cb7ac0e'
            '93a0b2e1b8181818ade215f00937db2decea79639147dcad2eb9faa8d669f4e4'
            'fb163aa1ba382e2a6009c8e1b468494b5ab11aa80c500012590226f1fb554040'
            '0d4ac2390db55ffd34be0222ab8de693620cd479149ba787ac13eb0125ffb5f2'
            'b4113d322513cdc13a487233b77cc5a7d487e551f8db4496056a3c1dd41659f5'
            '9484899dc57aacacae666a7b66416fe4d424372bb6a9540677cd596a21edc1fe'
            '5ee0f6f807e3b83f6a43826c9130b001232ab8d42eb664e4087daeca3853ac98'
            'f339e3e576ce94d7812c2887622c5882ae12ac3c9a98059aef37741850a43cb6'
            '670031115992cc59dcd9b9890847cf6a1705fd430518115aebe4514a02241a6e'
            '4d0113cf8b7a32eb38e8d6f51744fa8c8627539be01428a11573f9eaab127ba6'
            'e46155245bfa5734dbce9e4a272cc60a4b98f3fc4fd53dd3109104432881809e'
            '21d1e3c6c7df073e9b2696e00c4558becf0b1f9945ca00ad8e64302899d1fc9b'
            'bb1ff999bca9352af32caca4e92cfd5516954957dd9d4397b3f4bbcde26f8302'
            '56c7808d7b1d734474badada16af4432dc4b48d8db06918607b5bee47a25405c'
            '5c59bb724fab032656ebb69cf638abb2f40fcdbe9f6d01b5e3842fe2325989aa')

package() {
    # DTSC
    install -Dm755 "$srcdir/dtsc.py" "$pkgdir/usr/bin/dtsc"

    # RKDUMP
    install -Dm755 "$srcdir/rkdump.sh" "$pkgdir/usr/bin/rkdump"

    # Wakeupctl
    install -Dm755 "$srcdir/wakeupctl.py" "$pkgdir/usr/bin/wakeupctl"

    # beryl-Chroot
    install -Dm755 "$srcdir/beryllium-chroot.sh" "$pkgdir/usr/bin/beryllium-chroot"
    ln -s "/usr/bin/beryllium-chroot" "$pkgdir/usr/bin/beryl-chroot"

    # lsmmc
    install -Dm755 "$srcdir/lsmmc.py" "$pkgdir/usr/bin/lsmmc"

    # GRUB Password
    install -Dm755 "$srcdir/grub-password.sh" "$pkgdir/usr/bin/grub-password"
    install -Dm755 "$srcdir/grub-apply-unrestrict.py" "$pkgdir/usr/bin/grub-apply-unrestrict"
    install -Dm644 "$srcdir/grub-unrestrict.hook" "$pkgdir/usr/share/libalpm/hooks/grub-unrestrict.hook"

    # Sleepctl
    install -Dm755 "$srcdir/sleepctl.py" "$pkgdir/usr/bin/sleepctl"
    install -Dm755 "$srcdir/sleepctld.sh" "$pkgdir/usr/bin/sleepctld"
    install -Dm644 "$srcdir/sleepctl.service" "$pkgdir/usr/lib/systemd/user/sleepctl.service"

    # rv2rk
    install -Dm755 "$srcdir/rv2rk.py" "$pkgdir/usr/bin/rv2rk"
    mkdir "$pkgdir/usr/share/rv2rk"
    install -Dm644 "$srcdir/FSBL.bin" "$pkgdir/usr/share/rv2rk/FSBL.bin"
    install -Dm644 "$srcdir/u-boot.itb" "$pkgdir/usr/share/rv2rk/u-boot.itb"

    # rvflasher
    install -Dm755 "$srcdir/rvflasher.py" "$pkgdir/usr/bin/rvflasher"
    mkdir "$pkgdir/usr/share/rvflasher"
    install -Dm644 "$srcdir/FSBL_FLASH.bin" "$pkgdir/usr/share/rvflasher/FSBL.bin"
    install -Dm644 "$srcdir/u-boot_FLASH.itb" "$pkgdir/usr/share/rvflasher/u-boot.itb"
    install -Dm644 "$srcdir/partition_universal.json" "$pkgdir/usr/share/rvflasher/partition_universal.json"
    install -Dm644 "$srcdir/bootinfo_sd.bin" "$pkgdir/usr/share/rvflasher/bootinfo_sd.bin"

    # beryllium-fde
    install -Dm755 "$srcdir/beryllium-fde.py" "$pkgdir/usr/bin/beryllium-fde"

    # Manual pages
    install -Dm644 "$srcdir/rkdump.1" "$pkgdir/usr/share/man/man1/rkdump.1"
    install -Dm644 "$srcdir/grub-password.1" "$pkgdir/usr/share/man/man1/grub-password.1"
    install -Dm644 "$srcdir/dtsc.1" "$pkgdir/usr/share/man/man1/dtsc.1"
    install -Dm644 "$srcdir/sleepctl.1" "$pkgdir/usr/share/man/man1/sleepctl.1"
    install -Dm644 "$srcdir/sleepctld.1" "$pkgdir/usr/share/man/man1/sleepctld.1"
    install -Dm644 "$srcdir/wakeupctl.1" "$pkgdir/usr/share/man/man1/wakeupctl.1"
    install -Dm644 "$srcdir/beryllium-chroot.8" "$pkgdir/usr/share/man/man8/beryllium-chroot.8"
    install -Dm644 "$srcdir/beryllium-fde.8" "$pkgdir/usr/share/man/man8/beryllium-fde.8"
}
