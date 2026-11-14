"""Shared Agent Skills packages. Uploads never execute code or install dependencies."""
import io
import json
import os
from pathlib import Path,PurePosixPath
import re
import stat
import tempfile
import threading
import zipfile
import zlib

MAX_UPLOAD=5*1024*1024
MAX_UNPACKED=20*1024*1024
MAX_MARKDOWN=1024*1024
_lock=threading.RLock()


def metadata(text,strict=False):
    front=re.match(r'\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---(?:\r?\n|$)',text,re.S)
    header=front.group(1) if front else text
    def field(name):
        match=re.search(r'^'+name+r':[ \t]*(.*)$',header,re.M)
        if not match:return ''
        value=match.group(1).strip()
        if value in ('|','>','|-','>-','|+','>+'):
            lines=[]
            for line in header[match.end():].splitlines():
                if line and not line[0].isspace():break
                if line.strip():lines.append(line.strip())
            return ' '.join(lines)
        if value.startswith('"'):
            try:return json.loads(value)
            except ValueError:pass
        return value.strip("'\"")
    name,description=field('name'),field('description')
    if strict:
        if not front or not isinstance(name,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',name):
            raise ValueError('В начале SKILL.md нужен YAML-блок с name: имя-скилла (до 64 символов a-z, 0-9, _ или -).')
        if not isinstance(description,str) or not description.strip() or len(description)>4000:
            raise ValueError('В YAML-блоке SKILL.md нужно непустое description: описание (до 4000 символов).')
    return {'name':name,'description':description if isinstance(description,str) else ''}


def _entries(filename,raw):
    if not raw or len(raw)>MAX_UPLOAD:raise ValueError('Скилл должен быть не больше 5 МБ.')
    if filename.lower().endswith('.md'):
        if len(raw)>MAX_MARKDOWN:raise ValueError('SKILL.md должен быть не больше 1 МБ.')
        return {'SKILL.md':(raw,0o644)}
    if not filename.lower().endswith('.zip'):
        raise ValueError('Загрузите SKILL.md или ZIP-архив одного скилла со скриптами и ресурсами.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            items={};total=0
            if len(archive.infolist())>2000:raise ValueError('В архиве слишком много файлов.')
            for entry in archive.infolist():
                path=PurePosixPath(entry.filename)
                mode=entry.external_attr>>16
                if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in entry.filename or ':' in path.parts[0]:
                    raise ValueError('В архиве есть недопустимый путь.')
                if stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR):
                    raise ValueError('Ссылки и специальные файлы в архиве не поддерживаются.')
                if entry.flag_bits&1:raise ValueError('Архив должен быть без пароля.')
                if '__MACOSX' in path.parts or entry.is_dir():continue
                total+=entry.file_size
                if total>MAX_UNPACKED:raise ValueError('После распаковки скилл должен занимать не больше 20 МБ.')
                if path in items:raise ValueError('В архиве есть повторяющиеся пути.')
                items[path]=entry
            roots=[p.parent for p in items if p.name=='SKILL.md']
            if len(roots)!=1:raise ValueError('В ZIP должен быть ровно один скилл с файлом SKILL.md.')
            root=roots[0];result={}
            for path,entry in items.items():
                if path.is_relative_to(root):
                    relative=str(path.relative_to(root))
                    if relative=='SKILL.md' and entry.file_size>MAX_MARKDOWN:
                        raise ValueError('SKILL.md должен быть не больше 1 МБ.')
                    result[relative]=(archive.read(entry),0o644|((entry.external_attr>>16)&0o111))
            return result
    except (zipfile.BadZipFile,zipfile.LargeZipFile,NotImplementedError,RuntimeError,EOFError,zlib.error):
        raise ValueError('Не удалось прочитать ZIP-архив скилла.') from None


def install(folder,filename,raw):
    entries=_entries(filename,raw)
    try:info=metadata(entries['SKILL.md'][0].decode('utf-8-sig'),strict=True)
    except UnicodeError:raise ValueError('SKILL.md должен быть текстом UTF-8.') from None
    folder=Path(folder)
    with _lock:
        folder.mkdir(parents=True,exist_ok=True)
        target=folder/info['name']
        if os.path.lexists(target):raise FileExistsError('Скилл с таким именем уже есть. Существующие файлы не перезаписаны.')
        with tempfile.TemporaryDirectory(prefix='.upload-',dir=folder) as temporary:
            stage=Path(temporary)
            for relative,(data,mode) in entries.items():
                dest=stage/relative
                dest.parent.mkdir(parents=True,exist_ok=True)
                with dest.open('xb') as out:out.write(data)
                dest.chmod(mode)
            stage.chmod(0o755)
            os.rename(stage,target)
    return info['name']
