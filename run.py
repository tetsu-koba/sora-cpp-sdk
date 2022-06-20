import subprocess
import logging
import os
import urllib.parse
import zipfile
import tarfile
import shutil
import platform
import argparse
import multiprocessing
import hashlib
from typing import Callable, NamedTuple, Optional, List, Union, Dict
if platform.system() == 'Windows':
    import winreg


logging.basicConfig(level=logging.DEBUG)


class ChangeDirectory(object):
    def __init__(self, cwd):
        self._cwd = cwd

    def __enter__(self):
        self._old_cwd = os.getcwd()
        logging.debug(f'pushd {self._old_cwd} --> {self._cwd}')
        os.chdir(self._cwd)

    def __exit__(self, exctype, excvalue, trace):
        logging.debug(f'popd {self._old_cwd} <-- {self._cwd}')
        os.chdir(self._old_cwd)
        return False


def cd(cwd):
    return ChangeDirectory(cwd)


def cmd(args, **kwargs):
    logging.debug(f'+{args} {kwargs}')
    if 'check' not in kwargs:
        kwargs['check'] = True
    if 'resolve' in kwargs:
        resolve = kwargs['resolve']
        del kwargs['resolve']
    else:
        resolve = True
    if resolve:
        args = [shutil.which(args[0]), *args[1:]]
    return subprocess.run(args, **kwargs)


# 標準出力をキャプチャするコマンド実行。シェルの `cmd ...` や $(cmd ...) と同じ
def cmdcap(args, **kwargs):
    # 3.7 でしか使えない
    # kwargs['capture_output'] = True
    kwargs['stdout'] = subprocess.PIPE
    kwargs['stderr'] = subprocess.PIPE
    kwargs['encoding'] = 'utf-8'
    return cmd(args, **kwargs).stdout.strip()


def rm_rf(path: str):
    if not os.path.exists(path):
        logging.debug(f'rm -rf {path} => path not found')
        return
    if os.path.isfile(path) or os.path.islink(path):
        os.remove(path)
        logging.debug(f'rm -rf {path} => file removed')
    if os.path.isdir(path):
        shutil.rmtree(path)
        logging.debug(f'rm -rf {path} => directory removed')


def mkdir_p(path: str):
    if os.path.exists(path):
        logging.debug(f'mkdir -p {path} => already exists')
        return
    os.makedirs(path, exist_ok=True)
    logging.debug(f'mkdir -p {path} => directory created')


if platform.system() == 'Windows':
    PATH_SEPARATOR = ';'
else:
    PATH_SEPARATOR = ':'


def add_path(path: str, is_after=False):
    logging.debug(f'add_path: {path}')
    if 'PATH' not in os.environ:
        os.environ['PATH'] = path
        return

    if is_after:
        os.environ['PATH'] = os.environ['PATH'] + PATH_SEPARATOR + path
    else:
        os.environ['PATH'] = path + PATH_SEPARATOR + os.environ['PATH']


def download(url: str, output_dir: Optional[str] = None, filename: Optional[str] = None) -> str:
    if filename is None:
        output_path = urllib.parse.urlparse(url).path.split('/')[-1]
    else:
        output_path = filename

    if output_dir is not None:
        output_path = os.path.join(output_dir, output_path)

    if os.path.exists(output_path):
        return output_path

    try:
        if shutil.which('curl') is not None:
            cmd(["curl", "-fLo", output_path, url])
        else:
            cmd(["wget", "-cO", output_path, url])
    except Exception:
        # ゴミを残さないようにする
        if os.path.exists(output_path):
            os.remove(output_path)
        raise

    return output_path


def read_version_file(path: str) -> Dict[str, str]:
    versions = {}

    lines = open(path).readlines()
    for line in lines:
        line = line.strip()

        # コメント行
        if line[:1] == '#':
            continue

        # 空行
        if len(line) == 0:
            continue

        [a, b] = map(lambda x: x.strip(), line.split('=', 2))
        versions[a] = b.strip('"')

    return versions


# dir 以下にある全てのファイルパスを、dir2 からの相対パスで返す
def enum_all_files(dir, dir2):
    for root, _, files in os.walk(dir):
        for file in files:
            yield os.path.relpath(os.path.join(root, file), dir2)


def versioned(func):
    def wrapper(version, version_file, *args, **kwargs):
        if 'ignore_version' in kwargs:
            if kwargs.get('ignore_version'):
                rm_rf(version_file)
            del kwargs['ignore_version']

        if os.path.exists(version_file):
            ver = open(version_file).read()
            if ver.strip() == version.strip():
                return

        r = func(version=version, *args, **kwargs)

        with open(version_file, 'w') as f:
            f.write(version)

        return r

    return wrapper


# アーカイブが単一のディレクトリに全て格納されているかどうかを調べる。
#
# 単一のディレクトリに格納されている場合はそのディレクトリ名を返す。
# そうでない場合は None を返す。
def _is_single_dir(infos: List[Union[zipfile.ZipInfo, tarfile.TarInfo]],
                   get_name: Callable[[Union[zipfile.ZipInfo, tarfile.TarInfo]], str],
                   is_dir: Callable[[Union[zipfile.ZipInfo, tarfile.TarInfo]], bool]) -> Optional[str]:
    # tarfile: ['path', 'path/to', 'path/to/file.txt']
    # zipfile: ['path/', 'path/to/', 'path/to/file.txt']
    # どちらも / 区切りだが、ディレクトリの場合、後ろに / が付くかどうかが違う
    dirname = None
    for info in infos:
        name = get_name(info)
        n = name.rstrip('/').find('/')
        if n == -1:
            # ルートディレクトリにファイルが存在している
            if not is_dir(info):
                return None
            dir = name.rstrip('/')
        else:
            dir = name[0:n]
        # ルートディレクトリに２個以上のディレクトリが存在している
        if dirname is not None and dirname != dir:
            return None
        dirname = dir

    return dirname


def is_single_dir_tar(tar: tarfile.TarFile) -> Optional[str]:
    return _is_single_dir(tar.getmembers(), lambda t: t.name, lambda t: t.isdir())


def is_single_dir_zip(zip: zipfile.ZipFile) -> Optional[str]:
    return _is_single_dir(zip.infolist(), lambda z: z.filename, lambda z: z.is_dir())


# 解凍した上でファイル属性を付与する
def _extractzip(z: zipfile.ZipFile, path: str):
    z.extractall(path)
    if platform.system() == 'Windows':
        return
    for info in z.infolist():
        if info.is_dir():
            continue
        filepath = os.path.join(path, info.filename)
        mod = info.external_attr >> 16
        if (mod & 0o120000) == 0o120000:
            # シンボリックリンク
            with open(filepath, 'r') as f:
                src = f.read()
            os.remove(filepath)
            with cd(os.path.dirname(filepath)):
                if os.path.exists(src):
                    os.symlink(src, filepath)
        if os.path.exists(filepath):
            # 普通のファイル
            os.chmod(filepath, mod & 0o777)


# zip または tar.gz ファイルを展開する。
#
# 展開先のディレクトリは {output_dir}/{output_dirname} となり、
# 展開先のディレクトリが既に存在していた場合は削除される。
#
# もしアーカイブの内容が単一のディレクトリであった場合、
# そのディレクトリは無いものとして展開される。
#
# つまりアーカイブ libsora-1.23.tar.gz の内容が
# ['libsora-1.23', 'libsora-1.23/file1', 'libsora-1.23/file2']
# であった場合、extract('libsora-1.23.tar.gz', 'out', 'libsora') のようにすると
# - out/libsora/file1
# - out/libsora/file2
# が出力される。
#
# また、アーカイブ libsora-1.23.tar.gz の内容が
# ['libsora-1.23', 'libsora-1.23/file1', 'libsora-1.23/file2', 'LICENSE']
# であった場合、extract('libsora-1.23.tar.gz', 'out', 'libsora') のようにすると
# - out/libsora/libsora-1.23/file1
# - out/libsora/libsora-1.23/file2
# - out/libsora/LICENSE
# が出力される。
def extract(file: str, output_dir: str, output_dirname: str, filetype: Optional[str] = None):
    path = os.path.join(output_dir, output_dirname)
    logging.info(f"Extract {file} to {path}")
    if filetype == 'gzip' or file.endswith('.tar.gz'):
        rm_rf(path)
        with tarfile.open(file) as t:
            dir = is_single_dir_tar(t)
            if dir is None:
                os.makedirs(path, exist_ok=True)
                t.extractall(path)
            else:
                logging.info(f"Directory {dir} is stripped")
                path2 = os.path.join(output_dir, dir)
                rm_rf(path2)
                t.extractall(output_dir)
                if path != path2:
                    logging.debug(f"mv {path2} {path}")
                    os.replace(path2, path)
    elif filetype == 'zip' or file.endswith('.zip'):
        rm_rf(path)
        with zipfile.ZipFile(file) as z:
            dir = is_single_dir_zip(z)
            if dir is None:
                os.makedirs(path, exist_ok=True)
                # z.extractall(path)
                _extractzip(z, path)
            else:
                logging.info(f"Directory {dir} is stripped")
                path2 = os.path.join(output_dir, dir)
                rm_rf(path2)
                # z.extractall(output_dir)
                _extractzip(z, output_dir)
                if path != path2:
                    logging.debug(f"mv {path2} {path}")
                    os.replace(path2, path)
    else:
        raise Exception('file should end with .tar.gz or .zip')


def clone_and_checkout(url, version, dir, fetch, fetch_force):
    if fetch_force:
        rm_rf(dir)

    if not os.path.exists(os.path.join(dir, '.git')):
        cmd(['git', 'clone', url, dir])
        fetch = True

    if fetch:
        with cd(dir):
            cmd(['git', 'fetch'])
            cmd(['git', 'reset', '--hard'])
            cmd(['git', 'clean', '-df'])
            cmd(['git', 'checkout', '-f', version])


def git_clone_shallow(url, hash, dir):
    rm_rf(dir)
    mkdir_p(dir)
    with cd(dir):
        cmd(['git', 'init'])
        cmd(['git', 'remote', 'add', 'origin', url])
        cmd(['git', 'fetch', '--depth=1', 'origin', hash])
        cmd(['git', 'reset', '--hard', 'FETCH_HEAD'])


@versioned
def install_webrtc(version, source_dir, install_dir, platform: str):
    win = platform.startswith("windows_")
    filename = f'webrtc.{platform}.{"zip" if win else "tar.gz"}'
    rm_rf(os.path.join(source_dir, filename))
    archive = download(
        f'https://github.com/shiguredo-webrtc-build/webrtc-build/releases/download/{version}/{filename}',
        output_dir=source_dir)
    rm_rf(os.path.join(install_dir, 'webrtc'))
    extract(archive, output_dir=install_dir, output_dirname='webrtc')


class WebrtcConfig(NamedTuple):
    webrtcbuild_fetch: bool
    webrtcbuild_fetch_force: bool
    webrtc_fetch: bool
    webrtc_fetch_force: bool
    webrtc_gen: bool
    webrtc_gen_force: bool
    webrtc_extra_gn_args: str
    webrtc_nobuild: bool


def build_install_webrtc(version, source_dir, build_dir, install_dir, platform, debug, config):
    webrtcbuild_source_dir = os.path.join(source_dir, 'webrtc-build')

    clone_and_checkout(url='https://github.com/shiguredo-webrtc-build/webrtc-build.git',
                       version=version,
                       dir=webrtcbuild_source_dir,
                       fetch=config.webrtcbuild_fetch,
                       fetch_force=config.webrtcbuild_fetch_force)

    with cd(webrtcbuild_source_dir):
        args = ['--source-dir', source_dir,
                '--build-dir', build_dir,
                '--webrtc-nobuild-ios-framework',
                '--webrtc-nobuild-android-aar']
        if debug:
            args += ['--debug']
        if config.webrtc_fetch:
            args += ['--webrtc-fetch']
        if config.webrtc_fetch_force:
            args += ['--webrtc-fetch-force']
        if config.webrtc_gen:
            args += ['--webrtc-gen']
        if config.webrtc_gen_force:
            args += ['--webrtc-gen-force']
        if len(config.webrtc_extra_gn_args) != 0:
            args += ['--webrtc-extra-gn-args', config.webrtc_extra_gn_args]
        if config.webrtc_nobuild:
            args += ['--webrtc-nobuild']

        cmd(['python3', 'run.py', 'build', platform, *args])

    # インクルードディレクトリを増やしたくないので、
    # __config_site を libc++ のディレクトリにコピーしておく
    libcxx_dir = os.path.join(source_dir, 'webrtc', 'src', 'buildtools', 'third_party', 'libc++')
    if not os.path.exists(os.path.join(libcxx_dir, 'trunk', 'include', '__config_site')):
        shutil.copyfile(os.path.join(libcxx_dir, '__config_site'),
                        os.path.join(libcxx_dir, 'trunk', 'include', '__config_site'))


class WebrtcInfo(NamedTuple):
    version_file: str
    webrtc_include_dir: str
    webrtc_library_dir: str
    clang_dir: str
    libcxx_dir: str


def get_webrtc_info(webrtcbuild: bool, source_dir: str, build_dir: str, install_dir: str) -> WebrtcInfo:
    webrtc_source_dir = os.path.join(source_dir, 'webrtc')
    webrtc_build_dir = os.path.join(build_dir, 'webrtc')
    webrtc_install_dir = os.path.join(install_dir, 'webrtc')

    if webrtcbuild:
        return WebrtcInfo(
            version_file=os.path.join(source_dir, 'webrtc-build', 'VERSION'),
            webrtc_include_dir=os.path.join(webrtc_source_dir, 'src'),
            webrtc_library_dir=os.path.join(webrtc_build_dir, 'obj')
            if platform.system() == 'Windows' else webrtc_build_dir, clang_dir=os.path.join(
                webrtc_source_dir, 'src', 'third_party', 'llvm-build', 'Release+Asserts'),
            libcxx_dir=os.path.join(webrtc_source_dir, 'src', 'buildtools', 'third_party', 'libc++', 'trunk'),)
    else:
        return WebrtcInfo(
            version_file=os.path.join(webrtc_install_dir, 'VERSIONS'),
            webrtc_include_dir=os.path.join(webrtc_install_dir, 'include'),
            webrtc_library_dir=os.path.join(install_dir, 'webrtc', 'lib'),
            clang_dir=os.path.join(install_dir, 'llvm', 'clang'),
            libcxx_dir=os.path.join(install_dir, 'llvm', 'libcxx'),
        )


@versioned
def install_rootfs(version, install_dir, conf, platform):
    rootfs_dir = os.path.join(install_dir, 'rootfs')
    rm_rf(rootfs_dir)
    if platform.target.os == 'jetson' and platform.build.arch == 'arm64':
        # dbus のセットアップに /dev/urandom が必要
        mkdir_p(os.path.join(rootfs_dir, 'dev'))
        dev_urandom = os.path.join(rootfs_dir, 'dev/urandom')
        cmd(['mknod', dev_urandom, 'c', '1', '9'])
    cmd(['multistrap', '--no-auth', '-a', 'arm64', '-d', rootfs_dir, '-f', conf])
    # 絶対パスのシンボリックリンクを相対パスに置き換えていく
    for dir, _, filenames in os.walk(rootfs_dir):
        for filename in filenames:
            linkpath = os.path.join(dir, filename)
            # symlink かどうか
            if not os.path.islink(linkpath):
                continue
            target = os.readlink(linkpath)
            # 絶対パスかどうか
            if not os.path.isabs(target):
                continue
            # rootfs_dir を先頭に付けることで、
            # rootfs の外から見て正しい絶対パスにする
            targetpath = rootfs_dir + target
            # 参照先の絶対パスが存在するかどうか
            if not os.path.exists(targetpath):
                continue
            # 相対パスに置き換える
            relpath = os.path.relpath(targetpath, dir)
            logging.debug(f'{linkpath[len(rootfs_dir):]} targets {target} to {relpath}')
            os.remove(linkpath)
            os.symlink(relpath, linkpath)

    # なぜかシンボリックリンクが登録されていないので作っておく
    link = os.path.join(rootfs_dir, 'usr', 'lib', 'aarch64-linux-gnu', 'tegra', 'libnvbuf_fdmap.so')
    file = os.path.join(rootfs_dir, 'usr', 'lib', 'aarch64-linux-gnu', 'tegra', 'libnvbuf_fdmap.so.1.0.0')
    if os.path.exists(file) and not os.path.exists(link):
        os.symlink(os.path.basename(file), link)


@versioned
def install_android_ndk(version, install_dir, source_dir):
    archive = download(
        f'https://dl.google.com/android/repository/android-ndk-{version}-linux.zip',
        source_dir)
    rm_rf(os.path.join(install_dir, 'android-ndk'))
    extract(archive, output_dir=install_dir, output_dirname='android-ndk')


@versioned
def install_android_sdk_cmdline_tools(version, install_dir, source_dir):
    archive = download(
        f'https://dl.google.com/android/repository/commandlinetools-linux-{version}_latest.zip',
        source_dir)
    tools_dir = os.path.join(install_dir, "android-sdk-cmdline-tools")
    rm_rf(tools_dir)
    extract(archive, output_dir=tools_dir, output_dirname='cmdline-tools')
    # ライセンスを許諾する
    cmd(['/bin/bash', '-c',
        f'yes | {os.path.join(tools_dir, "cmdline-tools", "bin", "sdkmanager")} --sdk_root={tools_dir} --licenses'])


@versioned
def install_llvm(version, install_dir,
                 tools_url, tools_commit,
                 libcxx_url, libcxx_commit,
                 buildtools_url, buildtools_commit):
    llvm_dir = os.path.join(install_dir, 'llvm')
    rm_rf(llvm_dir)
    mkdir_p(llvm_dir)
    with cd(llvm_dir):
        # tools の update.py を叩いて特定バージョンの clang バイナリを拾う
        git_clone_shallow(tools_url, tools_commit, 'tools')
        with cd('tools'):
            cmd(['python3',
                os.path.join('clang', 'scripts', 'update.py'),
                '--output-dir', os.path.join(llvm_dir, 'clang')])

        # 特定バージョンの libcxx を利用する
        git_clone_shallow(libcxx_url, libcxx_commit, 'libcxx')

        # __config_site のために特定バージョンの buildtools を取得する
        git_clone_shallow(buildtools_url, buildtools_commit, 'buildtools')
        with cd('buildtools'):
            cmd(['git', 'reset', '--hard', buildtools_commit])
        shutil.copyfile(os.path.join(llvm_dir, 'buildtools', 'third_party', 'libc++', '__config_site'),
                        os.path.join(llvm_dir, 'libcxx', 'include', '__config_site'))


@versioned
def install_boost(
        version: str, source_dir, build_dir, install_dir,
        debug: bool, cxx: str, cflags: List[str], cxxflags: List[str], linkflags: List[str],
        toolset, visibility, target_os, architecture,
        android_ndk, native_api_level):
    version_underscore = version.replace('.', '_')
    archive = download(
        f'https://boostorg.jfrog.io/artifactory/main/release/{version}/source/boost_{version_underscore}.tar.gz',
        source_dir)
    extract(archive, output_dir=build_dir, output_dirname='boost')
    with cd(os.path.join(build_dir, 'boost')):
        bootstrap = '.\\bootstrap.bat' if target_os == 'windows' else './bootstrap.sh'
        b2 = 'b2' if target_os == 'windows' else './b2'
        runtime_link = 'static' if target_os == 'windows' else 'shared'

        cmd([bootstrap])

        if target_os == 'iphone':
            # iOS の場合、シミュレータとデバイス用のライブラリを作って
            # lipo で結合する
            IOS_BUILD_TARGETS = [('x86_64', 'iphonesimulator'), ('arm64', 'iphoneos')]
            for arch, sdk in IOS_BUILD_TARGETS:
                clangpp = cmdcap(['xcodebuild', '-find', 'clang++'])
                sysroot = cmdcap(['xcrun', '--sdk', sdk, '--show-sdk-path'])
                boost_arch = 'x86' if arch == 'x86_64' else 'arm'
                with open('project-config.jam', 'w') as f:
                    f.write(f"using clang \
                        : iphone \
                        : {clangpp} -arch {arch} -isysroot {sysroot} \
                          -fembed-bitcode \
                          -mios-version-min=10.0 \
                          -fvisibility=hidden \
                        : <striper> <root>{sysroot} \
                        ; \
                        ")
                cmd([
                    b2,
                    'install',
                    '-d+0',
                    f'--build-dir={os.path.join(build_dir, "boost", f"build-{arch}-{sdk}")}',
                    f'--prefix={os.path.join(build_dir, "boost", f"install-{arch}-{sdk}")}',
                    '--with-json',
                    '--layout=system',
                    '--ignore-site-config',
                    f'variant={"debug" if debug else "release"}',
                    f'cflags={" ".join(cflags)}',
                    f'cxxflags={" ".join(cxxflags)}',
                    f'linkflags={" ".join(linkflags)}',
                    f'toolset={toolset}',
                    f'visibility={visibility}',
                    f'target-os={target_os}',
                    'address-model=64',
                    'link=static',
                    f'runtime-link={runtime_link}',
                    'threading=multi',
                    f'architecture={boost_arch}'])
            arch, sdk = IOS_BUILD_TARGETS[0]
            installed_path = os.path.join(build_dir, 'boost', f'install-{arch}-{sdk}')
            rm_rf(os.path.join(install_dir, 'boost'))
            cmd(['cp', '-r', installed_path, os.path.join(install_dir, 'boost')])

            for lib in enum_all_files(os.path.join(installed_path, 'lib'), os.path.join(installed_path, 'lib')):
                if not lib.endswith('.a'):
                    continue
                files = [os.path.join(build_dir, 'boost', f'install-{arch}-{sdk}', 'lib', lib)
                         for arch, sdk in IOS_BUILD_TARGETS]
                cmd(['lipo', '-create', '-output', os.path.join(install_dir, 'boost', 'lib', lib)] + files)
        elif target_os == 'android':
            # Android の場合、android-ndk を使ってビルドする
            with open('project-config.jam', 'w') as f:
                bin = os.path.join(android_ndk, 'toolchains', 'llvm', 'prebuilt', 'linux-x86_64', 'bin')
                sysroot = os.path.join(android_ndk, 'toolchains', 'llvm', 'prebuilt', 'linux-x86_64', 'sysroot')
                f.write(f"using clang \
                    : android \
                    : {os.path.join(bin, f'aarch64-linux-android{native_api_level}-clang++')} \
                      --sysroot={sysroot} \
                    : <archiver>{os.path.join(bin, 'llvm-ar')} \
                      <ranlib>{os.path.join(bin, 'llvm-ranlib')} \
                    ; \
                    ")
            cmd([
                b2,
                'install',
                '-d+0',
                f'--prefix={os.path.join(install_dir, "boost")}',
                '--with-json',
                '--layout=system',
                '--ignore-site-config',
                f'variant={"debug" if debug else "release"}',
                f'compileflags=--sysroot={sysroot}',
                f'cflags={" ".join(cflags)}',
                f'cxxflags={" ".join(cxxflags)}',
                f'linkflags={" ".join(linkflags)}',
                f'toolset={toolset}',
                f'visibility={visibility}',
                f'target-os={target_os}',
                'address-model=64',
                'link=static',
                f'runtime-link={runtime_link}',
                'threading=multi',
                'architecture=arm'])
        else:
            if len(cxx) != 0:
                with open('project-config.jam', 'w') as f:
                    f.write(f'using {toolset} : : {cxx} : ;')
            cmd([
                b2,
                'install',
                '-d+0',
                f'--prefix={os.path.join(install_dir, "boost")}',
                '--with-json',
                '--layout=system',
                '--ignore-site-config',
                f'variant={"debug" if debug else "release"}',
                f'cflags={" ".join(cflags)}',
                f'cxxflags={" ".join(cxxflags)}',
                f'linkflags={" ".join(linkflags)}',
                f'toolset={toolset}',
                f'visibility={visibility}',
                f'target-os={target_os}',
                'address-model=64',
                'link=static',
                f'runtime-link={runtime_link}',
                'threading=multi',
                f'architecture={architecture}'])


def cmake_path(path: str) -> str:
    return path.replace('\\', '/')


@versioned
def install_cmake(version, source_dir, install_dir, platform: str, ext):
    url = f'https://github.com/Kitware/CMake/releases/download/v{version}/cmake-{version}-{platform}.{ext}'
    path = download(url, source_dir)
    extract(path, install_dir, 'cmake')
    # Android で自前の CMake を利用する場合、ninja へのパスが見つけられない問題があるので、同じディレクトリに symlink を貼る
    # https://issuetracker.google.com/issues/206099937
    if platform.startswith('linux'):
        with cd(os.path.join(install_dir, 'cmake', 'bin')):
            cmd(['ln', '-s', '/usr/bin/ninja', 'ninja'])


@versioned
def install_cuda_windows(version, source_dir, build_dir, install_dir):
    rm_rf(os.path.join(build_dir, 'cuda'))
    rm_rf(os.path.join(install_dir, 'cuda'))
    if version == '10.2.89-1':
        url = 'http://developer.download.nvidia.com/compute/cuda/10.2/Prod/local_installers/cuda_10.2.89_441.22_win10.exe'  # noqa: E501
    else:
        raise f'Unknown CUDA version {version}'
    file = download(url, source_dir)

    mkdir_p(os.path.join(build_dir, 'cuda'))
    mkdir_p(os.path.join(install_dir, 'cuda'))
    with cd(os.path.join(build_dir, 'cuda')):
        cmd(['7z', 'x', file])
    os.rename(os.path.join(build_dir, 'cuda', 'nvcc'), os.path.join(install_dir, 'cuda', 'nvcc'))


@versioned
def install_libva(version, source_dir, build_dir, install_dir, env):
    libva_source_dir = os.path.join(source_dir, 'libva')
    libva_build_dir = os.path.join(build_dir, 'libva')
    libva_install_dir = os.path.join(install_dir, 'libva')
    rm_rf(libva_source_dir)
    rm_rf(libva_build_dir)
    rm_rf(libva_install_dir)
    git_clone_shallow('https://github.com/intel/libva.git', version, libva_source_dir)
    env = {**os.environ, **env}
    mkdir_p(libva_build_dir)
    with cd(libva_build_dir):
        cmd([os.path.join(libva_source_dir, 'autogen.sh'),
             '--enable-static',
             '--disable-shared',
             '--with-drivers-path=/usr/lib/x86_64-linux-gnu/dri',
             '--prefix', libva_install_dir],
            env=env)
        cmd(['make', f'-j{multiprocessing.cpu_count()}'])
        cmd(['make', 'install'])


@versioned
def install_msdk_windows(version, source_dir, install_dir):
    # ソースディレクトリは通常より１ディレクトリ深く作成する。
    # msdk/build にビルドしたバイナリが置かれることになるため。
    msdk_source_dir = os.path.join(source_dir, 'msdk', 'MediaSDK')
    msdk_build_dir = os.path.join(source_dir, 'msdk', 'build')
    msdk_install_dir = os.path.join(install_dir, 'msdk')
    rm_rf(msdk_source_dir)
    rm_rf(msdk_build_dir)
    rm_rf(msdk_install_dir)
    git_clone_shallow('https://github.com/Intel-Media-SDK/MediaSDK.git', version, msdk_source_dir)
    mkdir_p(os.path.join(msdk_install_dir, 'include'))
    mkdir_p(os.path.join(msdk_install_dir, 'lib'))
    shutil.copytree(os.path.join(msdk_source_dir, 'api', 'include'), os.path.join(msdk_install_dir, 'include', 'mfx'))

    regpath = "SOFTWARE\\WOW6432Node\\Microsoft\\Microsoft SDKs\\Windows\\v10.0"
    with winreg.OpenKeyEx(winreg.HKEY_LOCAL_MACHINE, regpath) as key:
        wsdk_version = winreg.QueryValueEx(key, "ProductVersion")[0]
    configs = [
        'Release',
        'Platform=x64',
        'PlatformToolset=v142',
        'SpectreMitigation=false',
        f'WindowsTargetPlatformVersion={wsdk_version}.0',
    ]
    cmd(['MSBuild', '/t:build', f"/p:Configuration={';'.join(configs)}",
         os.path.join(msdk_source_dir, 'api', 'mfx_dispatch', 'windows', 'libmfx_vs2015.vcxproj')])
    shutil.copyfile(os.path.join(msdk_build_dir, 'win_x64', 'Release', 'lib', 'libmfx_vs2015.lib'),
                    os.path.join(msdk_install_dir, 'lib', 'libmfx.lib'))


@versioned
def install_msdk_linux(version, source_dir, build_dir, install_dir, libva_installed_dir, cmake_args):
    msdk_source_dir = os.path.join(source_dir, 'msdk')
    msdk_build_dir = os.path.join(build_dir, 'msdk')
    msdk_install_dir = os.path.join(install_dir, 'msdk')
    rm_rf(msdk_source_dir)
    rm_rf(msdk_build_dir)
    rm_rf(msdk_install_dir)
    git_clone_shallow('https://github.com/Intel-Media-SDK/MediaSDK.git', version, msdk_source_dir)
    mkdir_p(msdk_build_dir)
    with cd(msdk_source_dir):
        # 共有ライブラリではなく静的ライブラリを作る
        files = cmdcap(['find', '.', '-name', 'CMakeLists.txt']).splitlines()
        for file in files:
            cmd(['sed', '-i', 's/SHARED/STATIC/g', file])

    with cd(msdk_build_dir):
        cmd(['cmake',
             f'-DCMAKE_INSTALL_PREFIX={cmake_path(msdk_install_dir)}',
             '-DCMAKE_BUILD_TYPE=Release',
             f'-DCMAKE_PREFIX_PATH={libva_installed_dir}',
             '-DBUILD_RUNTIME=OFF',
             '-DBUILD_SAMPLES=OFF',
             '-DBUILD_TUTORIALS=OFF',
             msdk_source_dir,
             *cmake_args])
        cmd(['cmake', '--build', '.', f'-j{multiprocessing.cpu_count()}'])
        cmd(['cmake', '--install', '.'])


class PlatformTarget(object):
    def __init__(self, os, osver, arch):
        self.os = os
        self.osver = osver
        self.arch = arch

    @property
    def package_name(self):
        if self.os == 'windows':
            return f'windows_{self.arch}'
        if self.os == 'macos':
            return f'macos_{self.arch}'
        if self.os == 'ubuntu':
            return f'ubuntu-{self.osver}_{self.arch}'
        if self.os == 'ios':
            return 'ios'
        if self.os == 'android':
            return 'android'
        if self.os == 'raspberry-pi-os':
            return f'raspberry-pi-os_{self.arch}'
        if self.os == 'jetson':
            return 'ubuntu-20.04_armv8_jetson'
        raise Exception('error')


def get_windows_osver():
    osver = platform.release()
    with winreg.OpenKeyEx(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion") as key:
        return osver + '.' + winreg.QueryValueEx(key, "ReleaseId")[0]


def get_macos_osver():
    platform.mac_ver()[0]


def get_build_platform() -> PlatformTarget:
    os = platform.system()
    if os == 'Windows':
        os = 'windows'
        osver = get_windows_osver()
    elif os == 'Darwin':
        os = 'macos'
        osver = get_macos_osver()
    elif os == 'Linux':
        release = read_version_file('/etc/os-release')
        os = release['NAME']
        if os == 'Ubuntu':
            os = 'ubuntu'
            osver = release['VERSION_ID']
        else:
            raise Exception(f'OS {os} not supported')
        pass
    else:
        raise Exception(f'OS {os} not supported')

    arch = platform.machine()
    if arch in ('AMD64', 'x86_64'):
        arch = 'x86_64'
    elif arch in ('aarch64', 'arm64'):
        arch = 'arm64'
    else:
        raise Exception(f'Arch {arch} not supported')

    return PlatformTarget(os, osver, arch)


SUPPORTED_BUILD_OS = [
    'windows',
    'macos',
    'ubuntu',
]
SUPPORTED_TARGET_OS = SUPPORTED_BUILD_OS + [
    'ios',
    'android',
    'raspberry-pi-os',
    'jetson'
]


class Platform(object):
    def _check(self, flag):
        if not flag:
            raise Exception('Not supported')

    def _check_platform_target(self, p: PlatformTarget):
        if p.os == 'raspberry-pi-os':
            self._check(p.arch in ('armv6', 'armv7', 'armv8'))
        elif p.os == 'jetson':
            self._check(p.arch == 'armv8')
        elif p.os in ('ios', 'android'):
            self._check(p.arch is None)
        else:
            self._check(p.arch in ('x86_64', 'arm64'))

    def __init__(self, target_os, target_osver, target_arch):
        build = get_build_platform()
        target = PlatformTarget(target_os, target_osver, target_arch)

        self._check(build.os in SUPPORTED_BUILD_OS)
        self._check(target.os in SUPPORTED_TARGET_OS)

        self._check_platform_target(build)
        self._check_platform_target(target)

        if target.os == 'windows':
            self._check(target.arch == 'x86_64')
            self._check(build.os == 'windows')
            self._check(build.arch == 'x86_64')
        if target.os == 'macos':
            self._check(build.os == 'macos')
            self._check(build.arch in ('x86_64', 'arm64'))
        if target.os == 'ios':
            self._check(build.os == 'macos')
            self._check(build.arch in ('x86_64', 'arm64'))
        if target.os == 'android':
            self._check(build.os == 'ubuntu')
            self._check(build.arch == 'x86_64')
        if target.os == 'ubuntu':
            self._check(build.os == 'ubuntu')
            self._check(build.arch == 'x86_64')
            self._check(build.osver == target.osver)
        if target.os == 'raspberry-pi-os':
            self._check(build.os == 'ubuntu')
            self._check(build.arch == 'x86_64')
        if target.os == 'jetson':
            self._check(build.os == 'ubuntu')
            self._check(build.arch in ('x86_64', 'arm64'))

        self.build = build
        self.target = target


BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def install_deps(platform: Platform, source_dir, build_dir, install_dir, debug,
                 webrtcbuild: bool, webrtc_config: WebrtcConfig):
    with cd(BASE_DIR):
        version = read_version_file('VERSION')

        # multistrap を使った sysroot の構築
        if platform.target.os == 'jetson':
            conf = os.path.join(BASE_DIR, 'multistrap', f'{platform.target.package_name}.conf')
            # conf ファイルのハッシュ値をバージョンとする
            version_md5 = hashlib.md5(open(conf, 'rb').read()).hexdigest()
            install_rootfs_args = {
                'version': version_md5,
                'version_file': os.path.join(install_dir, 'rootfs.version'),
                'install_dir': install_dir,
                'conf': conf,
                'platform': platform,
            }
            install_rootfs(**install_rootfs_args)

        # Android NDK
        if platform.target.os == 'android':
            install_android_ndk_args = {
                'version': version['ANDROID_NDK_VERSION'],
                'version_file': os.path.join(install_dir, 'android-ndk.version'),
                'source_dir': source_dir,
                'install_dir': install_dir,
            }
            install_android_ndk(**install_android_ndk_args)

        # Android SDK Commandline Tools
        if platform.target.os == 'android':
            if 'ANDROID_SDK_ROOT' in os.environ and os.path.exists(os.environ['ANDROID_SDK_ROOT']):
                # 既に Android SDK が設定されている場合はインストールしない
                pass
            else:
                install_android_sdk_cmdline_tools_args = {
                    'version': version['ANDROID_SDK_CMDLINE_TOOLS_VERSION'],
                    'version_file': os.path.join(install_dir, 'android-sdk-cmdline-tools.version'),
                    'source_dir': source_dir,
                    'install_dir': install_dir,
                }
                install_android_sdk_cmdline_tools(**install_android_sdk_cmdline_tools_args)
                add_path(os.path.join(install_dir, 'android-sdk-cmdline-tools', 'cmdline-tools', 'bin'))
                os.environ['ANDROID_SDK_ROOT'] = os.path.join(install_dir, 'android-sdk-cmdline-tools')

        # WebRTC
        if platform.target.os == 'windows':
            webrtc_platform = f'windows_{platform.target.arch}'
        elif platform.target.os == 'macos':
            webrtc_platform = f'macos_{platform.target.arch}'
        elif platform.target.os == 'ios':
            webrtc_platform = 'ios'
        elif platform.target.os == 'android':
            webrtc_platform = 'android'
        elif platform.target.os == 'ubuntu':
            webrtc_platform = f'ubuntu-{platform.target.osver}_{platform.target.arch}'
        elif platform.target.os == 'raspberry-pi-os':
            webrtc_platform = f'raspberry-pi-os_{platform.target.arch}'
        elif platform.target.os == 'jetson':
            webrtc_platform = 'ubuntu-20.04_armv8'
        else:
            raise Exception(f'Unknown platform {platform.target.os}')

        if webrtcbuild:
            install_webrtc_args = {
                'version': version['WEBRTC_BUILD_VERSION'],
                'source_dir': source_dir,
                'build_dir': build_dir,
                'install_dir': install_dir,
                'platform': webrtc_platform,
                'debug': debug,
                'config': webrtc_config,
            }

            build_install_webrtc(**install_webrtc_args)
        else:
            install_webrtc_args = {
                'version': version['WEBRTC_BUILD_VERSION'],
                'version_file': os.path.join(install_dir, 'webrtc.version'),
                'source_dir': source_dir,
                'install_dir': install_dir,
                'platform': webrtc_platform,
            }

            install_webrtc(**install_webrtc_args)

        webrtc_info = get_webrtc_info(webrtcbuild, source_dir, build_dir, install_dir)
        webrtc_version = read_version_file(webrtc_info.version_file)

        # Windows は MSVC を使うので不要
        # macOS と iOS は Apple Clang を使うので不要
        if platform.target.os not in ('windows', 'macos', 'ios') and not webrtcbuild:
            # LLVM
            tools_url = webrtc_version['WEBRTC_SRC_TOOLS_URL']
            tools_commit = webrtc_version['WEBRTC_SRC_TOOLS_COMMIT']
            libcxx_url = webrtc_version['WEBRTC_SRC_BUILDTOOLS_THIRD_PARTY_LIBCXX_TRUNK_URL']
            libcxx_commit = webrtc_version['WEBRTC_SRC_BUILDTOOLS_THIRD_PARTY_LIBCXX_TRUNK_COMMIT']
            buildtools_url = webrtc_version['WEBRTC_SRC_BUILDTOOLS_URL']
            buildtools_commit = webrtc_version['WEBRTC_SRC_BUILDTOOLS_COMMIT']
            install_llvm_args = {
                'version':
                    f'{tools_url}.{tools_commit}.'
                    f'{libcxx_url}.{libcxx_commit}.'
                    f'{buildtools_url}.{buildtools_commit}',
                'version_file': os.path.join(install_dir, 'llvm.version'),
                'install_dir': install_dir,
                'tools_url': tools_url,
                'tools_commit': tools_commit,
                'libcxx_url': libcxx_url,
                'libcxx_commit': libcxx_commit,
                'buildtools_url': buildtools_url,
                'buildtools_commit': buildtools_commit,
            }
            install_llvm(**install_llvm_args)

        # Boost
        install_boost_args = {
            'version': version['BOOST_VERSION'],
            'version_file': os.path.join(install_dir, 'boost.version'),
            'source_dir': source_dir,
            'build_dir': build_dir,
            'install_dir': install_dir,
            'cxx': '',
            'cflags': [],
            'cxxflags': [],
            'linkflags': [],
            'toolset': '',
            'visibility': 'global',
            'target_os': '',
            'debug': debug,
            'android_ndk': '',
            'native_api_level': '',
            'architecture': 'x86',
        }
        if platform.target.os == 'windows':
            install_boost_args['cxxflags'] = [
                '-D_HAS_ITERATOR_DEBUGGING=0'
            ]
            install_boost_args['toolset'] = 'msvc'
            install_boost_args['target_os'] = 'windows'
        elif platform.target.os == 'macos':
            sysroot = cmdcap(['xcrun', '--sdk', 'macosx', '--show-sdk-path'])
            install_boost_args['target_os'] = 'darwin'
            install_boost_args['toolset'] = 'clang'
            install_boost_args['cxx'] = 'clang++'
            install_boost_args['cflags'] = [
                f"--sysroot={sysroot}",
            ]
            install_boost_args['cxxflags'] = [
                '-fPIC',
                f"--sysroot={sysroot}",
                '-std=gnu++17'
            ]
            install_boost_args['visibility'] = 'hidden'
            if platform.target.arch == 'x86_64':
                install_boost_args['cflags'] += ['-target', 'x86_64-apple-darwin']
                install_boost_args['cxxflags'] += ['-target', 'x86_64-apple-darwin']
                install_boost_args['architecture'] = 'x86'
            if platform.target.arch == 'arm64':
                install_boost_args['cflags'] += ['-target', 'aarch64-apple-darwin']
                install_boost_args['cxxflags'] += ['-target', 'aarch64-apple-darwin']
                install_boost_args['architecture'] = 'arm'
        elif platform.target.os == 'ios':
            install_boost_args['target_os'] = 'iphone'
            install_boost_args['toolset'] = 'clang'
            install_boost_args['cxxflags'] = [
                '-std=gnu++17'
            ]
        elif platform.target.os == 'android':
            install_boost_args['target_os'] = 'android'
            install_boost_args['cflags'] = [
                '-fPIC',
            ]
            install_boost_args['cxxflags'] = [
                '-fPIC',
                '-D_LIBCPP_ABI_UNSTABLE',
                '-D_LIBCPP_DISABLE_AVAILABILITY',
                '-nostdinc++',
                '-std=gnu++17',
                f"-isystem{os.path.join(webrtc_info.libcxx_dir, 'include')}",
            ]
            install_boost_args['toolset'] = 'clang'
            install_boost_args['android_ndk'] = os.path.join(install_dir, 'android-ndk')
            install_boost_args['native_api_level'] = version['ANDROID_NATIVE_API_LEVEL']
        elif platform.target.os == 'jetson':
            sysroot = os.path.join(install_dir, 'rootfs')
            install_boost_args['target_os'] = 'linux'
            if platform.build.arch == 'x86_64':
                install_boost_args['cxx'] = os.path.join(webrtc_info.clang_dir, 'bin', 'clang++')
            else:
                install_boost_args['cxx'] = 'clang++'
            install_boost_args['cflags'] = [
                '-fPIC',
                f"--sysroot={sysroot}",
                '--target=aarch64-linux-gnu',
                f"-I{os.path.join(sysroot, 'usr', 'include', 'aarch64-linux-gnu')}",
            ]
            install_boost_args['cxxflags'] = [
                '-fPIC',
                '--target=aarch64-linux-gnu',
                f"--sysroot={sysroot}",
                f"-I{os.path.join(sysroot, 'usr', 'include', 'aarch64-linux-gnu')}",
                '-D_LIBCPP_ABI_UNSTABLE',
                '-D_LIBCPP_DISABLE_AVAILABILITY',
                '-nostdinc++',
                '-std=gnu++17',
                f"-isystem{os.path.join(webrtc_info.libcxx_dir, 'include')}",
            ]
            install_boost_args['linkflags'] = [
                f"-L{os.path.join(sysroot, 'usr', 'lib', 'aarch64-linux-gnu')}",
                f"-B{os.path.join(sysroot, 'usr', 'lib', 'aarch64-linux-gnu')}",
            ]
            install_boost_args['toolset'] = 'clang'
            install_boost_args['architecture'] = 'arm'
        else:
            install_boost_args['target_os'] = 'linux'
            install_boost_args['cxx'] = os.path.join(webrtc_info.clang_dir, 'bin', 'clang++')
            install_boost_args['cxxflags'] = [
                '-D_LIBCPP_ABI_UNSTABLE',
                '-D_LIBCPP_DISABLE_AVAILABILITY',
                '-nostdinc++',
                f"-isystem{os.path.join(webrtc_info.libcxx_dir, 'include')}",
                '-fPIC',
            ]
            install_boost_args['toolset'] = 'clang'

        install_boost(**install_boost_args)

        # CMake
        install_cmake_args = {
            'version': version['CMAKE_VERSION'],
            'version_file': os.path.join(install_dir, 'cmake.version'),
            'source_dir': source_dir,
            'install_dir': install_dir,
            'platform': '',
            'ext': 'tar.gz'
        }
        if platform.build.os == 'windows' and platform.build.arch == 'x86_64':
            install_cmake_args['platform'] = 'windows-x86_64'
            install_cmake_args['ext'] = 'zip'
        elif platform.build.os == 'macos':
            install_cmake_args['platform'] = 'macos-universal'
        elif platform.build.os == 'ubuntu' and platform.build.arch == 'x86_64':
            install_cmake_args['platform'] = 'linux-x86_64'
        elif platform.build.os == 'ubuntu' and platform.build.arch == 'arm64':
            install_cmake_args['platform'] = 'linux-aarch64'
        else:
            raise Exception('Failed to install CMake')
        install_cmake(**install_cmake_args)

        if platform.build.os == 'macos':
            add_path(os.path.join(install_dir, 'cmake', 'CMake.app', 'Contents', 'bin'))
        else:
            add_path(os.path.join(install_dir, 'cmake', 'bin'))

        # CUDA
        if platform.target.os == 'windows':
            install_cuda_args = {
                'version': version['CUDA_VERSION'],
                'version_file': os.path.join(install_dir, 'cuda.version'),
                'source_dir': source_dir,
                'build_dir': build_dir,
                'install_dir': install_dir,
            }
            install_cuda_windows(**install_cuda_args)

        # Intel Media SDK
        if platform.target.os == 'windows' and platform.target.arch == 'x86_64':
            # 普通のビルド環境ならこれでパスが通るはず
            # GitHub Actions は microsoft/setup-msbuild で既にパスが通ってるので問題ない
            add_path('C:\\Program Files (x86)\\Microsoft Visual Studio\\2019\\Community\\MSBuild\\Current\\Bin')
            install_msdk_args = {
                'version': version['MSDK_VERSION'],
                'version_file': os.path.join(install_dir, 'msdk.version'),
                'source_dir': source_dir,
                'install_dir': install_dir,
            }
            install_msdk_windows(**install_msdk_args)
        if platform.target.os == 'ubuntu' and platform.target.arch == 'x86_64':
            install_libva_args = {
                'version': version['LIBVA_VERSION'],
                'version_file': os.path.join(install_dir, 'libva.version'),
                'source_dir': source_dir,
                'build_dir': build_dir,
                'install_dir': install_dir,
                'env': {
                    'CC': 'clang-12',
                    'CFLAGS': '-fPIC',
                },
            }
            install_libva(**install_libva_args)

            cxxflags = [
                '-nostdinc++',
                f"-isystem{os.path.join(webrtc_info.libcxx_dir, 'include')}",
                '-D_LIBCPP_ABI_UNSTABLE',
                '-D_LIBCPP_DISABLE_AVAILABILITY',
            ]
            install_msdk_args = {
                'version': version['MSDK_VERSION'],
                'version_file': os.path.join(install_dir, 'msdk.version'),
                'source_dir': source_dir,
                'build_dir': build_dir,
                'install_dir': install_dir,
                'libva_installed_dir': os.path.join(install_dir, 'libva'),
                'cmake_args': [
                    "-DCMAKE_C_COMPILER=clang-12",
                    "-DCMAKE_CXX_COMPILER=clang++-12",
                    f"-DCMAKE_CXX_FLAGS={' '.join(cxxflags)}",
                ]
            }
            install_msdk_linux(**install_msdk_args)

        if platform.target.os == 'android':
            # Android 側からのコールバックする関数は消してはいけないので、
            # libwebrtc.a の中から消してはいけない関数の一覧を作っておく
            #
            # readelf を使って libwebrtc.a の関数一覧を列挙して、その中から Java_org_webrtc_ を含む関数を取り出し、
            # -Wl,--undefined=<関数名> に加工する。
            # （-Wl,--undefined はアプリケーションから参照されていなくても関数を削除しないためのフラグ）
            readelf = os.path.join(
                install_dir, 'android-ndk', 'toolchains', 'llvm', 'prebuilt', 'linux-x86_64', 'bin', 'llvm-readelf')
            libwebrtc = os.path.join(webrtc_info.webrtc_library_dir, 'arm64-v8a', 'libwebrtc.a')
            m = cmdcap([readelf, '-Ws', libwebrtc])
            ldflags = []
            for line in m.splitlines():
                if line.find('Java_org_webrtc_') == -1:
                    continue
                # この時点で line は以下のような文字列になっている
                #    174: 0000000000000000    44 FUNC    GLOBAL DEFAULT    15 Java_org_webrtc_DataChannel_nativeClose
                func = line.split()[7]
                ldflags.append(f'-Wl,--undefined={func}')
            with open(os.path.join(install_dir, 'webrtc.ldflags'), 'w') as f:
                f.write('\n'.join(ldflags))


AVAILABLE_TARGETS = ['windows_x86_64', 'macos_x86_64', 'macos_arm64', 'ubuntu-20.04_x86_64',
                     'ubuntu-22.04_x86_64', 'ubuntu-20.04_armv8_jetson', 'ios', 'android']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=AVAILABLE_TARGETS)
    parser.add_argument("--debug", action='store_true')
    parser.add_argument("--webrtcbuild", action='store_true')
    parser.add_argument("--webrtcbuild-fetch", action='store_true')
    parser.add_argument("--webrtcbuild-fetch-force", action='store_true')
    parser.add_argument("--webrtc-fetch", action='store_true')
    parser.add_argument("--webrtc-fetch-force", action='store_true')
    parser.add_argument("--webrtc-gen", action='store_true')
    parser.add_argument("--webrtc-gen-force", action='store_true')
    parser.add_argument("--webrtc-extra-gn-args", default='')
    parser.add_argument("--webrtc-nobuild", action='store_true')
    parser.add_argument("--test", action='store_true')
    parser.add_argument("--run", action='store_true')
    parser.add_argument("--package", action='store_true')

    args = parser.parse_args()
    if args.target == 'windows_x86_64':
        platform = Platform('windows', get_windows_osver(), 'x86_64')
    elif args.target == 'macos_x86_64':
        platform = Platform('macos', get_macos_osver(), 'x86_64')
    elif args.target == 'macos_arm64':
        platform = Platform('macos', get_macos_osver(), 'arm64')
    elif args.target == 'ubuntu-20.04_x86_64':
        platform = Platform('ubuntu', '20.04', 'x86_64')
    elif args.target == 'ubuntu-22.04_x86_64':
        platform = Platform('ubuntu', '22.04', 'x86_64')
    elif args.target == 'ubuntu-20.04_armv8_jetson':
        platform = Platform('jetson', None, 'armv8')
    elif args.target == 'ios':
        platform = Platform('ios', None, None)
    elif args.target == 'android':
        platform = Platform('android', None, None)
    else:
        raise Exception(f'Unknown target {args.target}')

    logging.info(f'Build platform: {platform.build.package_name}')
    logging.info(f'Target platform: {platform.target.package_name}')

    configuration = 'debug' if args.debug else 'release'
    dir = platform.target.package_name
    source_dir = os.path.join(BASE_DIR, '_source', dir, configuration)
    build_dir = os.path.join(BASE_DIR, '_build', dir, configuration)
    install_dir = os.path.join(BASE_DIR, '_install', dir, configuration)
    package_dir = os.path.join(BASE_DIR, '_package', dir, configuration)
    mkdir_p(source_dir)
    mkdir_p(build_dir)
    mkdir_p(install_dir)

    install_deps(platform, source_dir, build_dir, install_dir, args.debug,
                 webrtcbuild=args.webrtcbuild, webrtc_config=args)

    configuration = 'Debug' if args.debug else 'Release'

    sora_build_dir = os.path.join(build_dir, 'sora')
    mkdir_p(sora_build_dir)
    with cd(sora_build_dir):
        cmake_args = []
        cmake_args.append(f'-DCMAKE_BUILD_TYPE={configuration}')
        cmake_args.append(f"-DCMAKE_INSTALL_PREFIX={cmake_path(os.path.join(install_dir, 'sora'))}")
        cmake_args.append(f"-DBOOST_ROOT={cmake_path(os.path.join(install_dir, 'boost'))}")
        webrtc_info = get_webrtc_info(args.webrtcbuild, source_dir, build_dir, install_dir)
        webrtc_version = read_version_file(webrtc_info.version_file)
        with cd(BASE_DIR):
            version = read_version_file('VERSION')
            sora_cpp_sdk_version = version['SORA_CPP_SDK_VERSION']
            sora_cpp_sdk_commit = cmdcap(['git', 'rev-parse', 'HEAD'])
            android_native_api_level = version['ANDROID_NATIVE_API_LEVEL']
        cmake_args.append(f"-DWEBRTC_INCLUDE_DIR={cmake_path(webrtc_info.webrtc_include_dir)}")
        cmake_args.append(f"-DWEBRTC_LIBRARY_DIR={cmake_path(webrtc_info.webrtc_library_dir)}")
        cmake_args.append(f"-DSORA_CPP_SDK_VERSION={sora_cpp_sdk_version}")
        cmake_args.append(f"-DSORA_CPP_SDK_COMMIT={sora_cpp_sdk_commit}")
        cmake_args.append(f"-DSORA_CPP_SDK_TARGET={platform.target.package_name}")
        cmake_args.append(f"-DWEBRTC_BUILD_VERSION={webrtc_version['WEBRTC_BUILD_VERSION']}")
        cmake_args.append(f"-DWEBRTC_READABLE_VERSION={webrtc_version['WEBRTC_READABLE_VERSION']}")
        cmake_args.append(f"-DWEBRTC_COMMIT={webrtc_version['WEBRTC_COMMIT']}")
        if platform.target.os == 'ubuntu':
            if platform.target.package_name in ('ubuntu-20.04_x86_64', 'ubuntu-22.04_x86_64'):
                cmake_args.append("-DCMAKE_C_COMPILER=clang-12")
                cmake_args.append("-DCMAKE_CXX_COMPILER=clang++-12")
            else:
                cmake_args.append(
                    f"-DCMAKE_C_COMPILER={cmake_path(os.path.join(webrtc_info.clang_dir, 'bin', 'clang'))}")
                cmake_args.append(
                    f"-DCMAKE_CXX_COMPILER={cmake_path(os.path.join(webrtc_info.clang_dir, 'bin', 'clang++'))}")
            cmake_args.append("-DUSE_LIBCXX=ON")
            cmake_args.append(
                f"-DLIBCXX_INCLUDE_DIR={cmake_path(os.path.join(webrtc_info.libcxx_dir, 'include'))}")
        if platform.target.os == 'macos':
            sysroot = cmdcap(['xcrun', '--sdk', 'macosx', '--show-sdk-path'])
            target = 'x86_64-apple-darwin' if platform.target.arch == 'x86_64' else 'aarch64-apple-darwin'
            cmake_args.append(f'-DCMAKE_SYSTEM_PROCESSOR={platform.target.arch}')
            cmake_args.append(f'-DCMAKE_OSX_ARCHITECTURES={platform.target.arch}')
            cmake_args.append(f'-DCMAKE_C_COMPILER_TARGET={target}')
            cmake_args.append(f'-DCMAKE_CXX_COMPILER_TARGET={target}')
            cmake_args.append(f'-DCMAKE_OBJCXX_COMPILER_TARGET={target}')
            cmake_args.append(f'-DCMAKE_SYSROOT={sysroot}')
        if platform.target.os == 'ios':
            cmake_args += ['-G', 'Xcode']
            cmake_args.append("-DCMAKE_SYSTEM_NAME=iOS")
            cmake_args.append("-DCMAKE_OSX_ARCHITECTURES=x86_64;arm64")
            cmake_args.append("-DCMAKE_OSX_DEPLOYMENT_TARGET=10.0")
            cmake_args.append("-DCMAKE_XCODE_ATTRIBUTE_ONLY_ACTIVE_ARCH=NO")
        if platform.target.os == 'android':
            toolchain_file = os.path.join(install_dir, 'android-ndk', 'build', 'cmake', 'android.toolchain.cmake')
            cmake_args.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}")
            cmake_args.append(f"-DANDROID_NATIVE_API_LEVEL={android_native_api_level}")
            cmake_args.append('-DANDROID_ABI=arm64-v8a')
            cmake_args.append('-DANDROID_STL=none')
            cmake_args.append("-DUSE_LIBCXX=ON")
            cmake_args.append(
                f"-DLIBCXX_INCLUDE_DIR={cmake_path(os.path.join(webrtc_info.libcxx_dir, 'include'))}")
            cmake_args.append('-DANDROID_CPP_FEATURES=exceptions rtti')
            # r23b には ANDROID_CPP_FEATURES=exceptions でも例外が設定されない問題がある
            # https://github.com/android/ndk/issues/1618
            cmake_args.append('-DCMAKE_ANDROID_EXCEPTIONS=ON')
            cmake_args.append(f"-DSORA_WEBRTC_LDFLAGS={os.path.join(install_dir, 'webrtc.ldflags')}")
        if platform.target.os == 'jetson':
            sysroot = os.path.join(install_dir, 'rootfs')
            cmake_args.append('-DCMAKE_SYSTEM_NAME=Linux')
            cmake_args.append('-DCMAKE_SYSTEM_PROCESSOR=aarch64')
            cmake_args.append(f'-DCMAKE_SYSROOT={sysroot}')
            cmake_args.append('-DCMAKE_C_COMPILER_TARGET=aarch64-linux-gnu')
            cmake_args.append('-DCMAKE_CXX_COMPILER_TARGET=aarch64-linux-gnu')
            cmake_args.append(f'-DCMAKE_FIND_ROOT_PATH={sysroot}')
            cmake_args.append("-DUSE_LIBCXX=ON")
            cmake_args.append(
                f"-DLIBCXX_INCLUDE_DIR={cmake_path(os.path.join(webrtc_info.libcxx_dir, 'include'))}")
            if platform.build.arch == 'x86_64':
                cmake_args.append(
                    f"-DCMAKE_C_COMPILER={cmake_path(os.path.join(webrtc_info.clang_dir, 'bin', 'clang'))}")
                cmake_args.append(
                    f"-DCMAKE_CXX_COMPILER={cmake_path(os.path.join(webrtc_info.clang_dir, 'bin', 'clang++'))}")
            cmake_args.append('-DUSE_JETSON_ENCODER=ON')

        # NvCodec
        if platform.target.os in ('windows', 'ubuntu') and platform.target.arch == 'x86_64':
            cmake_args.append('-DUSE_NVCODEC_ENCODER=ON')
            if platform.target.os == 'windows':
                cmake_args.append(f"-DCUDA_TOOLKIT_ROOT_DIR={cmake_path(os.path.join(install_dir, 'cuda', 'nvcc'))}")

        if platform.target.os in ('windows', 'ubuntu') and platform.target.arch == 'x86_64':
            cmake_args.append('-DUSE_MSDK_ENCODER=ON')
            cmake_args.append(f"-DMSDK_ROOT_DIR={cmake_path(os.path.join(install_dir, 'msdk'))}")
            if platform.target.os == 'ubuntu':
                cmake_args.append(f"-DLIBVA_ROOT_DIR={cmake_path(os.path.join(install_dir, 'libva'))}")

        cmd(['cmake', BASE_DIR] + cmake_args)
        if platform.target.os == 'ios':
            cmd(['cmake', '--build', '.', f'-j{multiprocessing.cpu_count()}', '--config', configuration,
                '--target', 'sora', '--', '-arch', 'x86_64', '-sdk', 'iphonesimulator'])
            cmd(['cmake', '--build', '.', f'-j{multiprocessing.cpu_count()}', '--config', configuration,
                '--target', 'sora', '--', '-arch', 'arm64', '-sdk', 'iphoneos'])
            # 後でライブラリは差し替えるけど、他のデータをコピーするためにとりあえず install は呼んでおく
            cmd(['cmake', '--install', '.'])
            cmd(['lipo', '-create', '-output', os.path.join(build_dir, 'sora', 'libsora.a'),
                os.path.join(build_dir, 'sora', f'{configuration}-iphonesimulator', 'libsora.a'),
                os.path.join(build_dir, 'sora', f'{configuration}-iphoneos', 'libsora.a')])
            shutil.copyfile(os.path.join(build_dir, 'sora', 'libsora.a'),
                            os.path.join(install_dir, 'sora', 'lib', 'libsora.a'))
        else:
            cmd(['cmake', '--build', '.', f'-j{multiprocessing.cpu_count()}', '--config', configuration])
            cmd(['cmake', '--install', '.', '--config', configuration])

        # バンドルされたライブラリをインストールする
        if platform.target.os == 'windows':
            shutil.copyfile(os.path.join(sora_build_dir, 'bundled', 'sora.lib'),
                            os.path.join(install_dir, 'sora', 'lib', 'sora.lib'))
        elif platform.target.os == 'ubuntu':
            shutil.copyfile(os.path.join(sora_build_dir, 'bundled', 'libsora.a'),
                            os.path.join(install_dir, 'sora', 'lib', 'libsora.a'))

    if args.test:
        if platform.target.os == 'ios':
            # iOS の場合は事前に用意したプロジェクトをビルドする
            cmd(['xcodebuild', 'build',
                '-project', 'test/ios/hello.xcodeproj',
                 '-target', 'hello',
                 '-arch', 'x86_64',
                 '-sdk', 'iphonesimulator',
                 '-configuration', 'Release'])
            # こっちは signing が必要になるのでやらない
            # cmd(['xcodebuild', 'build',
            #      '-project', 'test/ios/hello.xcodeproj',
            #      '-target', 'hello',
            #      '-arch', 'arm64',
            #      '-sdk', 'iphoneos',
            #      '-configuration', 'Release'])
        elif platform.target.os == 'android':
            # Android の場合は事前に用意したプロジェクトをビルドする
            with cd(os.path.join(BASE_DIR, 'test', 'android')):
                cmd(['./gradlew', '--no-daemon', 'assemble'])
        else:
            # 普通のプロジェクトは CMake でビルドする
            test_build_dir = os.path.join(build_dir, 'test')
            mkdir_p(test_build_dir)
            with cd(test_build_dir):
                cmake_args = []
                cmake_args.append(f'-DCMAKE_BUILD_TYPE={configuration}')
                cmake_args.append(f"-DBOOST_ROOT={cmake_path(os.path.join(install_dir, 'boost'))}")
                cmake_args.append(f"-DWEBRTC_INCLUDE_DIR={cmake_path(webrtc_info.webrtc_include_dir)}")
                cmake_args.append(f"-DWEBRTC_LIBRARY_DIR={cmake_path(webrtc_info.webrtc_library_dir)}")
                cmake_args.append(f"-DSORA_DIR={cmake_path(os.path.join(install_dir, 'sora'))}")
                if platform.target.os == 'macos':
                    sysroot = cmdcap(['xcrun', '--sdk', 'macosx', '--show-sdk-path'])
                    target = 'x86_64-apple-darwin' if platform.target.arch == 'x86_64' else 'aarch64-apple-darwin'
                    cmake_args.append(f'-DCMAKE_SYSTEM_PROCESSOR={platform.target.arch}')
                    cmake_args.append(f'-DCMAKE_OSX_ARCHITECTURES={platform.target.arch}')
                    cmake_args.append(f'-DCMAKE_C_COMPILER_TARGET={target}')
                    cmake_args.append(f'-DCMAKE_CXX_COMPILER_TARGET={target}')
                    cmake_args.append(f'-DCMAKE_OBJCXX_COMPILER_TARGET={target}')
                    cmake_args.append(f'-DCMAKE_SYSROOT={sysroot}')
                if platform.target.os == 'ubuntu':
                    if platform.target.package_name in ('ubuntu-20.04_x86_64', 'ubuntu-22.04_x86_64'):
                        cmake_args.append("-DCMAKE_C_COMPILER=clang-12")
                        cmake_args.append("-DCMAKE_CXX_COMPILER=clang++-12")
                    else:
                        cmake_args.append(
                            f"-DCMAKE_C_COMPILER={cmake_path(os.path.join(webrtc_info.clang_dir, 'bin', 'clang'))}")
                        cmake_args.append(
                            f"-DCMAKE_CXX_COMPILER={cmake_path(os.path.join(webrtc_info.clang_dir, 'bin', 'clang++'))}")
                    cmake_args.append("-DUSE_LIBCXX=ON")
                    cmake_args.append(
                        f"-DLIBCXX_INCLUDE_DIR={cmake_path(os.path.join(webrtc_info.libcxx_dir, 'include'))}")
                if platform.target.os == 'jetson':
                    sysroot = os.path.join(install_dir, 'rootfs')
                    cmake_args.append('-DHELLO_JETSON=ON')
                    cmake_args.append('-DCMAKE_SYSTEM_NAME=Linux')
                    cmake_args.append('-DCMAKE_SYSTEM_PROCESSOR=aarch64')
                    cmake_args.append(f'-DCMAKE_SYSROOT={sysroot}')
                    cmake_args.append('-DCMAKE_C_COMPILER_TARGET=aarch64-linux-gnu')
                    cmake_args.append('-DCMAKE_CXX_COMPILER_TARGET=aarch64-linux-gnu')
                    cmake_args.append(f'-DCMAKE_FIND_ROOT_PATH={sysroot}')
                    cmake_args.append('-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER')
                    cmake_args.append('-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=BOTH')
                    cmake_args.append('-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=BOTH')
                    cmake_args.append('-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=BOTH')
                    cmake_args.append("-DUSE_LIBCXX=ON")
                    cmake_args.append(
                        f"-DLIBCXX_INCLUDE_DIR={cmake_path(os.path.join(webrtc_info.libcxx_dir, 'include'))}")
                    if platform.build.arch == 'x86_64':
                        cmake_args.append(
                            f"-DCMAKE_C_COMPILER={cmake_path(os.path.join(webrtc_info.clang_dir, 'bin', 'clang'))}")
                        cmake_args.append(
                            f"-DCMAKE_CXX_COMPILER={cmake_path(os.path.join(webrtc_info.clang_dir, 'bin', 'clang++'))}")
                cmd(['cmake', os.path.join(BASE_DIR, 'test')] + cmake_args)
                cmd(['cmake', '--build', '.', f'-j{multiprocessing.cpu_count()}', '--config', configuration])
                if args.run:
                    if platform.target.os == 'windows':
                        cmd([os.path.join(test_build_dir, configuration, 'hello.exe'),
                            os.path.join(BASE_DIR, 'test', '.testparam.json')])
                    else:
                        cmd([os.path.join(test_build_dir, 'hello'), os.path.join(BASE_DIR, 'test', '.testparam.json')])

    if args.package:
        mkdir_p(package_dir)
        rm_rf(os.path.join(package_dir, 'sora'))
        rm_rf(os.path.join(package_dir, 'sora.env'))

        with cd(BASE_DIR):
            version = read_version_file('VERSION')
            sora_cpp_sdk_version = version['SORA_CPP_SDK_VERSION']

        with cd(install_dir):
            if platform.target.os == 'windows':
                archive_name = f'sora-cpp-sdk-{sora_cpp_sdk_version}_{platform.target.package_name}.zip'
                archive_path = os.path.join(package_dir, archive_name)
                with zipfile.ZipFile(archive_path, 'w') as f:
                    for file in enum_all_files('sora', '.'):
                        f.write(filename=file, arcname=file)
                with open(os.path.join(package_dir, 'sora.env'), 'w') as f:
                    f.write('CONTENT_TYPE=application/zip\n')
                    f.write(f'PACKAGE_NAME={archive_name}\n')
            else:
                archive_name = f'sora-cpp-sdk-{sora_cpp_sdk_version}_{platform.target.package_name}.tar.gz'
                archive_path = os.path.join(package_dir, archive_name)
                with tarfile.open(archive_path, 'w:gz') as f:
                    for file in enum_all_files('sora', '.'):
                        f.add(name=file, arcname=file)
                with open(os.path.join(package_dir, 'sora.env'), 'w') as f:
                    f.write("CONTENT_TYPE=application/gzip\n")
                    f.write(f'PACKAGE_NAME={archive_name}\n')


if __name__ == '__main__':
    main()
