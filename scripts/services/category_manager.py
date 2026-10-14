"""Add category definitions only, without changing ledger assignments or rules."""
import json
import os
from pathlib import Path
import tempfile


def _name(value):
    if not isinstance(value,str):
        raise ValueError('Names must be text.')
    name=value.strip()
    if not name or len(name)>100 or any(ord(c)<32 or ord(c)==127 for c in name):
        raise ValueError('Use a name of 1–100 characters without line breaks or control characters.')
    return name


def add_definitions(path: Path, payload):
    if not isinstance(payload,dict):
        raise ValueError('A JSON object is required.')
    category=_name(payload.get('category'))
    values=payload.get('subcategories')
    if not isinstance(values,list) or not 1<=len(values)<=100:
        raise ValueError('Add at least one subcategory (maximum 100).')
    subs=[_name(value) for value in values]
    if len({s.casefold() for s in subs})!=len(subs):
        raise ValueError('Subcategory names must be distinct.')
    tree=json.loads(path.read_text(encoding='utf-8'))
    matches=[cat for cat in tree if cat.casefold()==category.casefold()]
    if matches and category!=matches[0]:
        raise ValueError('That category already exists with different capitalisation. Choose it from the list.')
    existing=tree.get(category,[])
    if any(sub.casefold() in {s.casefold() for s in existing} for sub in subs):
        raise ValueError('A subcategory with that name already exists in this category.')
    tree[category]=existing+subs
    staged=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                                         prefix='.categories-',suffix='.tmp',delete=False) as f:
            staged=Path(f.name)
            json.dump(tree,f,indent=2,ensure_ascii=False)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(staged,path)
    finally:
        if staged is not None and staged.exists():
            staged.unlink()
    return tree,category,subs
