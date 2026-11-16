import re
import xml.etree.ElementTree as ET

def title(subject):
    return subject.get('name_cn') or subject.get('name') or f"Bangumi {subject['id']}"

def add(node, name, value, **attrs):
    if value is not None and value != '':
        e = ET.SubElement(node, name, attrs)
        e.text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))
        return e

def xml(node):
    ET.indent(node, space='  ')
    data = ET.tostring(node, encoding='utf-8', xml_declaration=True)
    ET.fromstring(data)
    return data

def base(tag, bundle, poster=None):
    s = bundle['subject']
    n = ET.Element(tag)
    add(n, 'title', title(s)); add(n, 'originaltitle', s.get('name'))
    add(n, 'year', (s.get('date') or '')[:4]); add(n, 'premiered', s.get('date'))
    add(n, 'plot', s.get('summary')); add(n, 'genre', '动画')
    add(n, 'uniqueid', s['id'], type='bangumi', default='true')
    add(n, 'bangumiid', s['id']); add(n, 'website', f"https://bgm.tv/subject/{s['id']}")
    add(n, 'lockdata', 'true')
    if poster:
        add(n, 'thumb', poster, aspect='poster')
    for t in s.get('tags', [])[:12]:
        add(n, 'tag', t.get('name'))
    rating = s.get('rating') or {}
    if rating.get('score'):
        r = ET.SubElement(ET.SubElement(n, 'ratings'), 'rating', name='community', max='10', default='true')
        add(r, 'value', rating['score']); add(r, 'votes', rating.get('total'))
    for p in bundle.get('persons', []):
        tag = {'动画制作':'studio', '导演':'director', '脚本':'writer', '系列构成':'credits'}.get(p.get('relation'))
        if tag:
            add(n, tag, p.get('name'))
    for character in bundle.get('characters', [])[:40]:
        for actor in character.get('actors', [])[:2]:
            a = ET.SubElement(n, 'actor')
            add(a, 'name', actor.get('name')); add(a, 'role', character.get('name'))
    return n

def episode(subject, root_subject, ep, season, number, end=None):
    n = ET.Element('episodedetails')
    add(n, 'title', ep.get('name_cn') or ep.get('name') or f'第 {number} 集')
    add(n, 'originaltitle', ep.get('name')); add(n, 'plot', ep.get('desc'))
    add(n, 'aired', ep.get('airdate')); add(n, 'showtitle', title(root_subject))
    add(n, 'season', season); add(n, 'episode', number); add(n, 'episodeend', end)
    add(n, 'uniqueid', ep['id'], type='bangumi', default='true'); add(n, 'bangumiid', ep['id'])
    add(n, 'uniqueid', subject['id'], type='bangumisubject'); add(n, 'bangumisubjectid', subject['id'])
    add(n, 'website', f"https://bgm.tv/ep/{ep['id']}"); add(n, 'lockdata', 'true')
    return xml(n)
