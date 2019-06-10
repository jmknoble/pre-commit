from __future__ import unicode_literals

import hashlib
import logging
import os
import os.path
import pprint
import sys

from gitignore_parser import parse_gitignore

from pre_commit import git
from pre_commit.util import cmd_output


logger = logging.getLogger(__name__)


def _prune_directory_list(dirnames, prunedirs):
    if prunedirs is None:
        return
    prune_indices = []
    for i in range(len(dirnames)):
        if dirnames[i] in prunedirs:
            prune_indices.append(i)
    for i in reversed(prune_indices):  # avoid recomputing indices
        del dirnames[i]


def _exclusive_or(a, b):
    return (
        (not a and b) or
        (a and not b)
    )


def _ignore_pathname(pathname, ignore_func, invert):
    return _exclusive_or(invert, ignore_func(os.path.abspath(pathname)))


def _list_filesystem_tree(
        root,
        dirs=False,
        files=True,
        ignorefile=None,
        invert=False,
        prunedirs=None,
        sort=True,
):
    """Equivalent-ish to ``find ROOT [-type ...] -print``"""
    if ignorefile is not None:
        gitignore_matches = parse_gitignore(ignorefile)
    else:
        invert = False
        def gitignore_matches(x):
            return False

    all_pathnames = []
    for (prefix, dirnames, filenames) in os.walk(root, topdown=True, followlinks=False):
        _prune_directory_list(dirnames, prunedirs)
        all_pathnames.extend([
            x for x in [
                os.path.join(prefix, d) for d in dirnames if dirs
            ] + [
                os.path.join(prefix, f) for f in filenames if files
            ] if not _ignore_pathname(x, gitignore_matches, invert)
        ])
    if sort:
        all_pathnames.sort()
    return all_pathnames


def _hash_files(filenames):
    """Return a dict of {filename: digest} pairs"""
    digests = {}
    for filename in filenames:
        with open(filename, 'rb') as f:
            digests[filename] = hashlib.sha384(f.read()).hexdigest()
    return digests


def _diff_dicts(dict1, dict2):
    """Return a dict containing lists of added, removed, and changed keys"""
    added_keys = []
    removed_keys = []
    changed_keys = []
    for (key, value) in dict1.items():
        if key in dict2:
            if value != dict2[key]:
                changed_keys.append(key)
        else:
            removed_keys.append(key)
    for key in dict2:
        if key not in dict1:
            added_keys.append(key)
    return {'added': added_keys, 'removed': removed_keys, 'changed': changed_keys}


class GitAdapter(object):
    """Adapter to handle working in a git or non-git folder structure"""

    def __init__(self, without_git=False, root=None):
        self.without_git = without_git
        self.root = root

    def get_root(self):
        return self.root if self.without_git else git.get_root()

    def get_git_dir(self, git_root='.'):
        if self.without_git:
            raise NotImplementedError
        return git.get_git_dir(git_root)

    def get_remote_url(self, git_root):
        if self.without_git:
            raise NotImplementedError
        return git.get_remote_url(git_root)

    def is_in_merge_conflict(self):
        return False if self.without_git else git.is_in_merge_conflict()

    def parse_merge_msg_for_conflicts(self, merge_msg):
        return [] if self.without_git else git.parse_merge_msg_for_conflicts(merge_msg)

    def get_conflicted_files(self):
        return set() if self.without_git else git.get_conflicted_files()

    def get_staged_files(self, cwd=None):
        # TODO: Should this be nothing, or everything?  May depend on context...
        return [] if self.without_git else git.get_staged_files(cwd=cwd)

    def intent_to_add_files(self):
        if self.without_git:
            raise NotImplementedError
        return git.intent_to_add_files()

    def get_all_files(self):
        if self.without_git:
            ignorefile = os.path.join(self.root, '.gitignore')
            ignoredirs = [os.path.join(self.root, '.git')]
            return _list_filesystem_tree(
                self.root,
                ignorefile=ignorefile,
                ignoredirs=ignoredirs,
            )
        return git.get_all_files()

    def get_changed_files(self, new, old):
        # TODO: Should this be nothing, or everything?  May depend on context...
        return [] if self.without_git else git.get_changed_files(new, old)

    def get_diff(self):
        if self.without_git:
            return _hash_files(self.get_all_files())
        return git.get_diff()

    def set_diff_checkpoint(self):
        if self.without_git:
            self._diff_checkpoint = self.get_diff()

    def get_checkpointed_diff(self):
        if self.without_git:
            return _diff_dicts(self._diff_checkpoint, self.get_diff())

    def print_checkpointed_diff(self, color=False):
        if self.without_git:
            pprint.pprint(self.get_checkpointed_diff())
        else:
            git.print_diff(color)

    def head_rev(self, remote):
        if not self.without_git:
            raise NotImplementedError
        return git.head_rev(remote)

    def has_diff(self, *args, **kwargs):
        return 0 if self.without_git else git.has_diff(*args, **kwargs)

    def has_checkpointed_diff(self, *args, **kwargs):
        if self.without_git:
            diff_dict = self.get_checkpointed_diff()
            return (
                len(diff_dict['added']) > 0 or
                len(diff_dict['removed']) > 0 or
                len(diff_dict['changed']) > 0
            )
        return git.has_diff(*args, **kwargs)

    def has_unmerged_paths(self):
        return False if self.without_git else git.has_unmerged_paths()

    def has_unstaged_config(self, config_file):
        return False if self.without_git else git.has_unstaged_config(config_file)

    def commit(self, repo='.'):
        if self.without_git:
            raise NotImplementedError
        return git.commit(repo=repo)

    def git_path(self, name, repo='.'):
        if self.without_git:
            raise NotImplementedError
        return git.git_path(name, repo=repo)
