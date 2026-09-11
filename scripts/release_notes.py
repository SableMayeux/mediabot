"""Extract the exact tagged version's recorded release notes."""
import argparse
from pathlib import Path
import re


def release_notes(tag, changelog):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('A stable vMAJOR.MINOR.PATCH tag is required')
    section = re.search(r'^## \[' + re.escape(tag[1:]) + r'\] - [^\n]+\n(.*?)(?=^## \[|\Z)',
        changelog, re.MULTILINE | re.DOTALL)
    if not section or not section.group(1).strip():
        raise ValueError('Tagged version has no recorded changelog entry')
    return section.group(1).strip() + '\n\nSource archives correspond to this tag. The tag CI must pass before this release is published.\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(release_notes(args.tag, Path('CHANGELOG.md').read_text(encoding='utf-8')), encoding='utf-8')
