# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import shutil
import tempfile
import functools

from components.logging import LogLevel
from components.providerbase import BaseProvider, INeedsCommandProvider, INeedsLoggingProvider


class Commit:
    def __init__(self, pretty_line):
        parts = pretty_line.split("|")
        self.revision = parts[0]
        self.author_date = parts[1]
        self.commit_date = parts[2]

        self.files_modified = []
        self.files_added = []
        self.files_deleted = []
        self.files_other = []

    def populate_details(self, run):
        rev_range = [self.revision + "^", self.revision]

        files_changed = run(["git", "diff", "--name-status"] + rev_range).stdout.decode().split()
        for f in files_changed:
            f = f.strip()
            if not f:
                continue

            parts = f.split("\t")
            if parts[0] == 'M':
                self.files_modified.append(parts[1])
            elif parts[0] == 'A':
                self.files_added.append(parts[1])
            elif parts[0] == 'D':
                self.files_deleted.append(parts[1])
            else:
                self.files_other.append(parts[0] + " " + parts[1])

        self.summary = run(["git", "log", "--pretty=%s", "-1", self.revision]).stdout.decode()
        self.description = run(["git", "log", "--pretty=%b", "-1", self.revision]).stdout.decode()
        self.revision_link = "https://chromium.googlesource.com/angle/angle" + "/+/" + self.revision

    def __eq__(self, other):
        if isinstance(other, Commit):
            return self.revision == other.revision
        return False

    def __str__(self):
        return "Commit: " + self.revision


class SCMProvider(BaseProvider, INeedsCommandProvider, INeedsLoggingProvider):
    def __init__(self, config):
        pass

    def check_for_update(self, library, task, all_library_jobs):
        # This function uses two tricky variable names:
        #  all_upstream_commits - This means the commits that have occured upstream, on the branch we care about,
        #                         between the library's current revision and the tip of the branch.
        #
        #  unseen_new_upstream_commits - This means the commits that have occured upstream, on the branch we
        #                                care about, that have never been seen in Updatebot before.
        #
        #  The second is always a subset of the first. *However* the only way to figure out the second is by asking
        #  Updatebot 'what jobs (for this library) have you run before'? and examining the result. This is what
        #  Step 4 is about below.

        # Step 0: Get the repo and update to the correct branch.
        # If no branch is specified, the default branch we clone is assumed to be correct
        original_dir = os.getcwd()
        tmpdirname = tempfile.mkdtemp()
        os.chdir(tmpdirname)

        # This try block is used to ensure we clean up and chdir at the end always. It has no except clause,
        # exceptions raised are sent up the stack.
        try:

            self.run(["git", "clone", library.repo_url, "."])

            if task.branch:
                self.run(["git", "checkout", task.branch])

            # Step 1: Confirm that the current branch (the one we're tracking) contains
            # the current revision of the library. If it doesn't, that doesn't make sense.
            # (When the library revision changes, we must update 'branch' if we have moved to a new branch.)
            #
            # Note that git branch -r --contains would also work, and would list all the local
            # _and remote_ branches that contained the commit; but it is needlessly verbose.
            # Because we did a git checkout of the branch we care about, it will show up
            # without -r.
            current_branch = self.run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.decode().strip()
            self.logger.log("Our current branch is %s." % (current_branch), level=LogLevel.Debug)

            ret = self.run(["git", "branch", "--contains", library.revision])
            containing_branches = [line.replace("*", "").strip() for line in ret.stdout.decode().split()]
            self.logger.log("Containing branches are %s." % (containing_branches), level=LogLevel.Debug)

            if current_branch not in containing_branches:
                self.logger.log("Current Branch: %s" % current_branch, level=LogLevel.Error)
                self.logger.log("Branches (%s):" % len(containing_branches), level=LogLevel.Error)
                for b in containing_branches:
                    self.logger.log("  - %s" % b, level=LogLevel.Error)
                raise Exception("The current revision %s is not contained in the current branch %s." % (library.revision, current_branch))

            # Step 2: Get the list of commits between the revision and HEAD
            all_new_upstream_commits = self._commits_since(library.revision)
            if not all_new_upstream_commits:
                self.logger.log("Checking for updates to %s but no new upstream commits were found from our current in-tree revision %s." % (library.name, library.revision), level=LogLevel.Info)
                return []

            # We do have new upstream commits.
            # Step 3: Get the most recent job for the library
            most_recent_job = all_library_jobs[0] if all_library_jobs else None

            # Step 4: Check if the most recent job was performed on a revision _after_ the library's current revision or _before_.
            # 'After the library's current revision' means:
            #        in m-c we update the library to A
            #        then B was commited upstream
            #        we saw B and then ran a job for it
            #        Resulting in the most recent job being run after the library's current revision
            # 'Before the library's current revision' means:
            #        (the above happens)
            #        in m-c we update the library to B (or maybe even a new rev C with no job)
            #        Resulting in the most recent job (which was for B) occured _before_ the library's current revision
            # We can only do this if we have a most recent job, if we don't we're processing this library for the first time
            if most_recent_job:
                most_recent_job_newer_than_library_rev = most_recent_job.version in [c.revision for c in all_new_upstream_commits]
                if most_recent_job_newer_than_library_rev:
                    self.logger.log("The most recent job we have run is for a revision still upstream and not in the mozilla repo.", level=LogLevel.Debug)
                else:
                    self.logger.log("The most recent job we have run is older than the current revision in the mozilla repo.", level=LogLevel.Debug)
            else:
                self.logger.log("We've never run a job for this library before.", level=LogLevel.Debug)
                most_recent_job_newer_than_library_rev = False

            unseen_new_upstream_commits = []
            if most_recent_job_newer_than_library_rev:
                # Step 4: Get the list of commits between the revision for the most recent job
                # and HEAD
                unseen_new_upstream_commits = self._commits_since(most_recent_job.version)
                if len(unseen_new_upstream_commits) == 0:
                    self.logger.log("Already processed revision %s in bug %s" % (most_recent_job.version, most_recent_job.bugzilla_id), level=LogLevel.Info)
                    return []

                # Step 5: Ensure that the unseen list of a strict ordered subset of the 'all-new' list
                # Techinically this is optional; we could have started at Step 3. But this approach is
                # more conservative and will help us identify unexpected situations that may invalidate
                # our assumptions about how things should happen.
                offsetIndex = len(all_new_upstream_commits) - len(unseen_new_upstream_commits)
                self.logger.log("The first unseen upstream commit is offset %s entries into the %s upstream commits." % (offsetIndex, len(all_new_upstream_commits)), level=LogLevel.Debug)
                assert offsetIndex != len(all_new_upstream_commits), "Somehow the offset index is the length of the array even though we checked the length already"

                error_func = functools.partial(self._print_differing_commit_lists, all_new_upstream_commits, "all_new_upstream_commits", unseen_new_upstream_commits, "unseen_new_upstream_commits")
                if offsetIndex < 0:
                    error_func("There are more unseen upstream commits than new upstream commits??")
                if all_new_upstream_commits[offsetIndex:] != unseen_new_upstream_commits:
                    error_func("unseen_new_upstream_commits is not a strict ordered subset of all_new_upstream_commits")

            else:  # not most_recent_job_newer_than_library_rev
                # If the most recent job isn't in the list of all new upstream commits; then the entire
                # list of new upstream commits is the list of the unseen upstream commits.
                unseen_new_upstream_commits = all_new_upstream_commits

            # Step 6: Populate the second list with additional details about the commits
            [c.populate_details(self.run) for c in unseen_new_upstream_commits]

        finally:
            # Step 7 Return us to the origin directory and clean up
            os.chdir(original_dir)
            shutil.rmtree(tmpdirname)

        # Step 8: Return it
        return unseen_new_upstream_commits

    def _commits_since(self, revision):
        ret = self.run(["git", "log", "--pretty=%H|%ai|%ci", revision + "..HEAD"])
        commits = [line.strip() for line in ret.stdout.decode().split()]
        # Put them in order of oldest to newest
        commits.reverse()
        # Populate them into a class but don't get details just yet.
        return [Commit(c) for c in commits if c]

    def _print_differing_commit_lists(self, list_a, list_a_name, list_b, list_b_name, problem):
        self.logger.log("%s." % problem, level=LogLevel.Error)
        self.logger.log("%s (%s)" % (list_a_name, len(list_a)), level=LogLevel.Error)
        for c in list_a:
            self.logger.log("  - %s" % c, level=LogLevel.Error)
        self.logger.log("%s (%s)" % (list_b_name, len(list_b)), level=LogLevel.Error)
        for c in list_b:
            self.logger.log("  - %s" % c, level=LogLevel.Error)
        raise Exception(problem)

    def build_bug_description(self, list_of_commits):
        return str(list_of_commits)