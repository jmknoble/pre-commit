from __future__ import unicode_literals

import logging
import os
import re
import sys

from identify.identify import tags_from_path

from pre_commit import color
from pre_commit import output
from pre_commit.clientlib import load_config
from pre_commit.git_adapter import GitAdapter
from pre_commit.output import get_hook_message
from pre_commit.repository import all_hooks
from pre_commit.repository import install_hook_envs
from pre_commit.staged_files_only import staged_files_only
from pre_commit.util import cmd_output
from pre_commit.util import noop_context


logger = logging.getLogger('pre_commit')


def filter_by_include_exclude(names, include, exclude):
    include_re, exclude_re = re.compile(include), re.compile(exclude)
    return [
        filename for filename in names
        if include_re.search(filename)
        if not exclude_re.search(filename)
    ]


class Classifier(object):
    def __init__(self, filenames):
        self.filenames = [f for f in filenames if os.path.lexists(f)]
        self._types_cache = {}

    def _types_for_file(self, filename):
        try:
            return self._types_cache[filename]
        except KeyError:
            ret = self._types_cache[filename] = tags_from_path(filename)
            return ret

    def by_types(self, names, types, exclude_types):
        types, exclude_types = frozenset(types), frozenset(exclude_types)
        ret = []
        for filename in names:
            tags = self._types_for_file(filename)
            if tags >= types and not tags & exclude_types:
                ret.append(filename)
        return ret

    def filenames_for_hook(self, hook):
        names = self.filenames
        names = filter_by_include_exclude(names, hook.files, hook.exclude)
        names = self.by_types(names, hook.types, hook.exclude_types)
        return names


def _get_skips(environ):
    skips = environ.get('SKIP', '')
    return {skip.strip() for skip in skips.split(',') if skip.strip()}


def _hook_msg_start(hook, verbose):
    return '{}{}'.format('[{}] '.format(hook.id) if verbose else '', hook.name)


SKIPPED = 'Skipped'
NO_FILES = '(no files to check)'


def _run_single_hook(git_shim, classifier, hook, args, skips, cols):
    filenames = classifier.filenames_for_hook(hook)

    if hook.language == 'pcre':
        logger.warning(
            '`{}` (from {}) uses the deprecated pcre language.\n'
            'The pcre language is scheduled for removal in pre-commit 2.x.\n'
            'The pygrep language is a more portable (and usually drop-in) '
            'replacement.'.format(hook.id, hook.src),
        )

    if hook.id in skips or hook.alias in skips:
        output.write(
            get_hook_message(
                _hook_msg_start(hook, args.verbose),
                end_msg=SKIPPED,
                end_color=color.YELLOW,
                use_color=args.color,
                cols=cols,
            ),
        )
        return 0
    elif not filenames and not hook.always_run:
        output.write(
            get_hook_message(
                _hook_msg_start(hook, args.verbose),
                postfix=NO_FILES,
                end_msg=SKIPPED,
                end_color=color.TURQUOISE,
                use_color=args.color,
                cols=cols,
            ),
        )
        return 0

    # Print the hook and the dots first in case the hook takes hella long to
    # run.
    output.write(
        get_hook_message(
            _hook_msg_start(hook, args.verbose), end_len=6, cols=cols,
        ),
    )
    sys.stdout.flush()

    diff_before = git_shim.get_diff()
    retcode, stdout, stderr = hook.run(
        tuple(filenames) if hook.pass_filenames else (),
    )
    diff_after = git_shim.get_diff()

    file_modifications = diff_before != diff_after

    # If the hook makes changes, fail the commit
    if file_modifications:
        retcode = 1

    if retcode:
        retcode = 1
        print_color = color.RED
        pass_fail = 'Failed'
    else:
        retcode = 0
        print_color = color.GREEN
        pass_fail = 'Passed'

    output.write_line(color.format_color(pass_fail, print_color, args.color))

    if (
            (stdout or stderr or file_modifications) and
            (retcode or args.verbose or hook.verbose)
    ):
        output.write_line('hookid: {}\n'.format(hook.id))

        # Print a message if failing due to file modifications
        if file_modifications:
            output.write('Files were modified by this hook.')

            if stdout or stderr:
                output.write_line(' Additional output:')

            output.write_line()

        for out in (stdout, stderr):
            assert type(out) is bytes, type(out)
            if out.strip():
                output.write_line(out.strip(), logfile_name=hook.log_file)
        output.write_line()

    return retcode


def _compute_cols(hooks, verbose):
    """Compute the number of columns to display hook messages.  The widest
    that will be displayed is in the no files skipped case:

        Hook name...(no files to check) Skipped

    or in the verbose case

        Hook name [hookid]...(no files to check) Skipped
    """
    if hooks:
        name_len = max(len(_hook_msg_start(hook, verbose)) for hook in hooks)
    else:
        name_len = 0

    cols = name_len + 3 + len(NO_FILES) + 1 + len(SKIPPED)
    return max(cols, 80)


def _all_filenames(git_shim, args):
    if args.origin and args.source:
        return git_shim.get_changed_files(args.origin, args.source)
    elif args.hook_stage in {'prepare-commit-msg', 'commit-msg'}:
        return (args.commit_msg_filename,)
    elif args.files:
        return args.files
    elif args.all_files:
        return git_shim.get_all_files()
    elif git_shim.is_in_merge_conflict():
        return git_shim.get_conflicted_files()
    else:
        return git_shim.get_staged_files()


def _run_hooks(git_shim, config, hooks, args, environ):
    """Actually run the hooks."""
    git_shim.set_diff_checkpoint()
    skips = _get_skips(environ)
    cols = _compute_cols(hooks, args.verbose)
    filenames = _all_filenames(git_shim, args)
    filenames = filter_by_include_exclude(filenames, '', config['exclude'])
    classifier = Classifier(filenames)
    retval = 0
    for hook in hooks:
        retval |= _run_single_hook(git_shim, classifier, hook, args, skips, cols)
        if retval and config['fail_fast']:
            break
    if retval and args.show_diff_on_failure and git_shim.has_checkpointed_diff():
        if args.all_files:
            output.write_line(
                'pre-commit hook(s) made changes.\n'
                'If you are seeing this message in CI, '
                'reproduce locally with: `pre-commit run --all-files`.\n'
                'To run `pre-commit` as part of git workflow, use '
                '`pre-commit install`.',
            )
        output.write_line('All changes made by hooks:')
        # args.color is a boolean.
        # See user_color function in color.py
        git_shim.print_checkpointed_diff(args.color)
    return retval


def run(config_file, store, args, environ=os.environ):
    git_shim = GitAdapter(without_git=args.without_git, root=args.root)
    no_stash = args.all_files or bool(args.files)

    # Check if we have unresolved merge conflict files and fail fast.
    if git_shim.has_unmerged_paths():
        logger.error('Unmerged files.  Resolve before committing.')
        return 1
    if bool(args.source) != bool(args.origin):
        logger.error('Specify both --origin and --source.')
        return 1
    if git_shim.has_unstaged_config(config_file) and not no_stash:
        logger.error(
            'Your pre-commit configuration is unstaged.\n'
            '`git add {}` to fix this.'.format(config_file),
        )
        return 1

    # Expose origin / source as environment variables for hooks to consume
    if args.origin and args.source:
        environ['PRE_COMMIT_ORIGIN'] = args.origin
        environ['PRE_COMMIT_SOURCE'] = args.source

    if no_stash:
        ctx = noop_context()
    else:
        ctx = staged_files_only(store.directory)

    with ctx:
        config = load_config(config_file)
        hooks = [
            hook
            for hook in all_hooks(config, store)
            if not args.hook or hook.id == args.hook or hook.alias == args.hook
            if args.hook_stage in hook.stages
        ]

        if args.hook and not hooks:
            output.write_line('No hook with id `{}`'.format(args.hook))
            return 1

        install_hook_envs(hooks, store)

        return _run_hooks(git_shim, config, hooks, args, environ)
