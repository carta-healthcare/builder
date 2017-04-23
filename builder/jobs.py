"""Used to implement the basic framework around job nodes. Job nodes are
the nodes that can be called and will perform an action.
"""

import json
import collections

import arrow

import builder.expanders
import builder.targets
from builder.util import convert_to_timedelta

class Job(object):
    """A Job is a particular run of a JobDefinition.
    """
    def __init__(self, job, unique_id, build_graph, build_context,
                 meta=None):
        if meta is None:
            meta = {}
        self.job = job
        self.unique_id = unique_id
        self.build_graph = build_graph
        self.build_context = build_context
        self.meta = meta

        self.unexpanded_id = job.unexpanded_id
        self.config = job.config
        self.cache_time = job.cache_time

        # State
        self.retries = 0
        self.failed = False
        self.last_run = None
        self.stale = None
        self.buildable = None
        self.should_run = None
        self.parents_should_run = None
        self.expanded_directions = {"up": False, "down": False}
        self.is_running = False
        self.force = False

    def __repr__(self):
        return "{}:{}".format(self.unexpanded_id, self.unique_id)

    def invalidate(self):
        """Sets all cached values to their default None"""
        self.stale = None
        self.buildable = None
        self.should_run = None
        self.parents_should_run = None

    def reset(self):
        """Sets all values to their defaults"""
        self.invalidate()
        self.retries = 0
        self.last_run = None
        self.is_running = False
        self.force = False
        self.failed = False

    def get_stale_alternates(self):
        """Returns True if the job does not have an alternate or if any
        of it's alternates don't exist otherwise returns the mtimes of
        the alternates
        """
        targets = self.build_graph.get_target_relationships(self.unique_id)
        alternates = targets.get("alternates", [])

        alternate_mtimes = []
        for alternate_id in alternates:
            alternate = self.build_graph.get_target(alternate_id)
            if not alternate.get_exists():
                return True
            alternate_mtimes.append(alternate.get_mtime())
        if not alternates:
            return True
        return alternate_mtimes

    def update_stale(self, new_value):
        """Updates the stale value of the node and then updates all the above
        nodes.

        This is needed due to alternates. If the job above this job has an
        alternate that is this job's target, then the above job may not be
        stale when it's target doesn't exist.
        If this job is stale then it needs the targert from the above job.
        Therefore this job will then tell the above job that it needs to be
        stale. That is the goal of this function.

        If the new value is True and the old value was not True then
        everything above it is updated.
        Updating the above involves looking at all the dependencies.
        If a dependency doesn't exist, then it updates the job of the
        dependency to stale
        """
        if new_value == True and self.stale != True:
            self.stale = new_value
            dependency_ids = self.build_graph.get_dependency_ids(self.unique_id)
            for dependency_id in dependency_ids:
                dependency = self.build_graph.get_target(dependency_id)
                if not dependency.get_exists():
                    creator_ids = self.build_graph.get_creator_ids(dependency_id)
                    for creator_id in creator_ids:
                        creator = self.build_graph.get_job(creator_id)
                        creator.update_stale(True)
        self.stale = new_value

    def get_minimum_target_mtime(self):
        """Returns the minimum target mtime or returns True if a stale condition
        is met

        Stale conditions are the following:
            - There are no targets for the job
            - The job has no produces and an alternate is missing
            - The job is missing a produces and is missing an alternates or
                doesn't have an alternate

        Returns:
            True: if a stale condition is met
            Minimum mtime: if no stale condition is met the lowest mtime of
                the targets, returned
        """
        # The target doesn't produce anything so it only depends on it's
        # alternates
        target_dict = self.build_graph.get_target_relationships(self.unique_id)

        # There are no targets so it is just a cron job with dependencies
        if not target_dict:
            return True

        produced_targets = target_dict.get("produces")
        if not produced_targets:
            return self.get_stale_alternates()

        alt_check = False
        target_mtimes = [float("inf")]
        for target_id, data in produced_targets.items():
            target = self.build_graph.get_target(target_id)
            if not target.get_exists() and not alt_check:
                stale_alternates = self.get_stale_alternates()
                if stale_alternates == True:
                    return True
                target_mtimes = target_mtimes + stale_alternates
            else:
                if data.get("ignore_mtime", False):
                    continue
                target_mtimes.append(target.get_mtime())
        min_target_mtime = min(target_mtimes)
        return min_target_mtime

    def get_maximum_dependency_mtime(self, minimum_target_mtime):
        """Returns True if a dependency mtime is greater than the
        minimum_target_mtime
        """
        dependency_dict = self.build_graph.get_dependency_relationships(
                self.unique_id)
        for _, group_list in dependency_dict.items():
            for group_dict in group_list:
                if group_dict["data"].get("ignore_mtime", False):
                    continue
                for dependency_id in group_dict["targets"]:
                    dependency = self.build_graph.get_target(dependency_id)
                    if dependency.get_exists():
                        if dependency.get_mtime() > minimum_target_mtime:
                            return True
        return False

    def get_stale(self):
        """Returns whether or not the job needs to run to update it's output

        Often this job will look at the mtime of it's inputs and it's outputs
        and determine if the job needs to run

        Stale conditions:
            The job has been updated to stale with update_stale
            A target doesn't exist and the job doesn't have an alternate
            A target doesn't exist and a single alternate doesn't exist
            A target's mtime is lower than a dependency's mtime
            The job has no targets
            The job has no produces and is missing an alternates
        """
        if self.stale != None:
            return self.stale
        if not self.past_cache_time():
            self.stale = False
            return False

        minimum_target_mtime = self.get_minimum_target_mtime()
        if minimum_target_mtime is True:
            self.update_stale(True)
            return True

        greater_mtime = self.get_maximum_dependency_mtime(minimum_target_mtime)
        if greater_mtime:
            self.update_stale(True)
            return True

        self.update_stale(False)
        return False

    def set_stale(self, stale):
        self.stale = stale

    def get_buildable(self):
        """Returns whether or not the job is buildable

        Buildability is true when all the depends are met. This is true when
        all of the depends node's return True

        Buildable conditions:
            All the above dependency nodes return true
        """
        if self.buildable is not None:
            return self.buildable

        for dependency_node_id in self.build_graph.predecessors(self.unique_id):
            dependency_node = self.build_graph.node[dependency_node_id]
            dependency_func = dependency_node["object"].func
            buildable_ids = self.build_graph.predecessors(dependency_node_id)
            buildable_nodes = []
            for buildable_id in buildable_ids:
                buildable_nodes.append(
                    self.build_graph.node[buildable_id]["object"])
            buildable = dependency_func(buildable_nodes)
            if not buildable:
                self.buildable = False
                return False

        self.buildable = True
        return True

    def set_buildable(self, buildable):
        self.buildable = buildable

    def get_failed(self):
        return self.failed

    def past_cache_time(self):
        """Returns true if the job is past it's cache time

        This implementation returns true if the oldest mtime is older than
        the cache_time or if non of the targets exist
        """
        cache_time = self.cache_time
        if cache_time is None:
            return True
        cache_delta = convert_to_timedelta(cache_time)
        current_time = arrow.get()
        for target_edge in self.build_graph.out_edges(self.unique_id, data=True):
            if target_edge[2]["kind"] == "produces":
                target = self.build_graph.node[target_edge[1]]["object"]
                if not target.get_exists():
                    return True
                elif arrow.get(target.get_mtime()) + cache_delta < current_time:
                    return True
        return False

    def all_dependencies(self):
        """Returns whether or not all the jobs dependencies exist"""
        for depends_node_id in self.build_graph.predecessors(self.unique_id):
            for dependency_id in self.build_graph.predecessors(depends_node_id):
                dependency = self.build_graph.node[dependency_id]["object"]
                if not dependency.get_exists():
                    return False
        return True

    def past_curfew(self):
        """Returns whether or not the job is past it's curfew

        True by default
        """
        return True

    def get_parent_jobs(self):
        """Returns a list of all the parent jobs"""
        parent_jobs = []
        for depends_node_id in self.build_graph.predecessors(self.unique_id):
            for dependency_id in self.build_graph.predecessors(depends_node_id):
                parent_jobs = (parent_jobs +
                               self.build_graph.predecessors(dependency_id))
        return parent_jobs

    def update_lower_nodes_should_run(self, update_set=None):
        """Updates whether or not the job should run based off the new
        information on the referrer
        """
        if update_set is None:
            update_set = set([])

        if self.unique_id in update_set:
            return

        self.invalidate()
        self.get_should_run()
        for target_id in self.build_graph.neighbors(self.unique_id):
            for depends_id in self.build_graph.neighbors(target_id):
                for job_id in self.build_graph.neighbors(depends_id):
                    job = self.build_graph.node[job_id]["object"]
                    job.update_lower_nodes_should_run(update_set=update_set)

        update_set.add(self.unique_id)

    def set_failed(self, failed):
        """Sets the job as failed and sets the state that a failed job should
        have
        """
        if failed == True:
            self.failed = True
            self.force = False
            self.should_run = False

    def get_parents_should_run(self):
        """Returns whether or not any contiguous ancestor job with the
        same cache_time bool value should run

        False if an ancestor should run
        True if no ancestor should run
        """
        if self.parents_should_run is not None:
            return self.parents_should_run

        if self.should_ignore_parents():
            return False

        for dependency_id in self.get_parent_jobs():
            dependency = self.build_graph.node[dependency_id]["object"]
            if not dependency.should_ignore_parents():
                parents_should_run = dependency.get_parents_should_run()
                should_run_immediate = dependency.get_should_run_immediate()
                if parents_should_run or should_run_immediate:
                    self.parents_should_run = True
                    return True

        self.parents_should_run = False
        return False

    def get_force(self):
        return self.force


    def set_force(self, force):
        self.force = force


    def get_should_run_immediate(self):
        """Returns whether or not the node should run not caring about the
        ancestors should run status
        """
        if self.force or self.job.get_always_force():
            return True
        if self.get_failed():
            return False
        if self.should_run is not None:
            return self.should_run

        has_cache_time = self.cache_time is not None
        stale = self.get_stale()
        buildable = self.get_buildable()
        if not stale or not buildable:
            self.should_run = False
            return False

        past_curfew = self.past_curfew()
        all_dependencies = self.all_dependencies()
        if has_cache_time or past_curfew or all_dependencies:
            self.should_run = True
            return True
        self.should_run = False
        return False

    def get_should_run(self):
        """Returns whether or not the job should run

        depends on it's current state and whether or not it's ancestors
        should run
        """
        if self.force:
            return True

        if self.get_parents_should_run():
            return False


        return self.get_should_run_immediate()

    def set_should_run(self, should_run):
        self.should_run = should_run

    def should_ignore_parents(self):
        """
        Returns true if this job should ignore parents. E.g. if this job is set to run
        on a timeout
        """
        return self.cache_time is not None

    def get_command(self):
        """Returns the job's expanded command"""
        command_template = self.job.get_command(self.unique_id, self.build_context,
                                          self.build_graph)
        return self._replace_command(command_template, )

    def _replace_command(self, command):
        """Used to replace all of the formatting on the string used for
        recipes
        """
        got_format_args = False

        if "$@" in command:
            target_edges = self.build_graph.out_edges(self.unique_id)
            target_edges = filter(lambda x: self.build_graph.get_edge_data(*x).get('label') == 'produces', target_edges)
            targets = " ".join(map(lambda x: x[1], target_edges))
            command = command.replace("$@", targets)

        dependency_ids = self.build_graph.get_dependency_ids(self.unique_id)
        existing_dependency_ids = []
        for dependency_id in dependency_ids:
            dependency = self.build_graph.get_target(dependency_id)
            if dependency.get_exists():
                existing_dependency_ids.append(dependency_id)

        if "$^" in command:
            prerequisites_string = " ".join(existing_dependency_ids)
            command = command.replace("$^", prerequisites_string)

        if "$A" in command:
            user_args = self.build_context.get('user_args') or []
            user_args = " ".join(user_args)
            command = command.replace("$A", user_args)

        command = str(command)
        return command

    def get_id(self):
        """ Returns this Job's unique id
        """
        return self.unique_id

class TimestampExpandedJob(Job):
    def __init__(self, job, unique_id, build_graph, build_context):
        super(TimestampExpandedJob, self).__init__(job,
                unique_id, build_graph, build_context)
        self.curfew = job.curfew

    def past_curfew(self):
        time_delta = convert_to_timedelta(self.curfew)
        end_time = self.build_context["end_time"]
        curfew_time = end_time + time_delta
        return curfew_time < arrow.get()


class MetaJob(TimestampExpandedJob):

    def get_should_run_immediate(self):
        return False


class JobDefinition(object):
    """A job"""
    def __init__(self, unexpanded_id=None, cache_time=None, targets=None,
                 dependencies=None, command=None, config=None):
        if targets is None:
            targets = {}

        if dependencies is None:
            dependencies = {}

        if config is None:
            config = {}

        # Support setting unexpanded_id as class attribute
        if not (hasattr(self, 'unexpanded_id') and unexpanded_id is None):
            self.unexpanded_id = unexpanded_id
        self.cache_time = cache_time
        self.targets = targets
        self.dependencies = dependencies
        self.config = config
        self.command = command

    def get_id(self):
        """
        Returns a unique name for the job
        """
        return self.unexpanded_id

    def get_expandable_id(self):
        """Returns the unexpanded_id with any expansion neccessary information
        appended
        """
        return self.unexpanded_id

    def get_job_type(self):
        """Returns the type of state to use for expansions"""
        return Job

    def expand(self, build_graph, build_context):
        """Used to expand the node using a build context returns a list of
        nodes

        a typical expansion is a timestamp expansion where build
        context would use start time and end time and the node
        would expand from there
        """
        return [self.construct_job(self.get_expandable_id(), build_graph, build_context)]

    def construct_job(self, expanded_id, build_graph, build_context):
        """
        Return the Job instance to insert into the build graph.

        By default, make a Job instance using the
        Job type returned by get_job_type.
        """
        job_type = self.get_job_type()
        expanded_node = job_type(self, expanded_id, build_graph, build_context)
        return expanded_node

    def get_enabled(self):
        """Used to determine if the node should end up in the build graph
        or not. For example, when the deployment doesn't have backbone
        no backbone node should be in the graph
        """
        return True

    def get_command(self, unique_id, build_context, build_graph):
        """Used to get the command related to the command"""
        return self.command

    def get_dependencies(self):
        """most jobs will depend on the existance of a file, this is what is
        returned here. It is in the form
        {
            "dependency_type": [
                dependency_class,
            ],
        }
        """
        return self.dependencies

    def get_targets(self):
        """most jobs will output a target, specify them here
        form:
            {
                "target_type": [
                    target_class
                ],
            }
        """
        return self.targets

    def get_always_force(self):
        return False

    def run(self):
        """
        Override this function to execute python jobs
        :return:
        """
        raise NotImplementedError("Run function not implemented")

    def __repr__(self):
        dependencies_dict = self.get_dependencies()
        targets_dict = self.get_targets()

        str_dependencies = collections.defaultdict(list)
        for dependency_type, dependencies in dependencies_dict.items():
            for dependency in dependencies:
                str_dependencies[dependency_type].append(dependency.unexpanded_id)

        str_targets = collections.defaultdict(list)
        for target_type, targets in targets_dict.items():
            for target in targets:
                str_targets[target_type].append(target.unexpanded_id)

        this_dict = {"depends": str_dependencies, "targets": str_targets}

        return str(json.dumps(this_dict, indent=2))

def new_expand_wrapper(old_expand, target_mtime):
    def new_expand(*args, **kwargs):
        targets = old_expand(*args, **kwargs)
        for target in targets:
            target.mtime = target_mtime
            target.cached_mtime = True
        return targets
    return new_expand

class SimpleTestJobDefinition(JobDefinition):
    """A simple API for creating a job through constructor args"""


    def __init__(self, unexpanded_id=None, targets=None, depends=None,
            config=None, should_run=False, parents_should_run=False,
            target_type=None, expander_type=None,
            depends_dict=None, targets_dict=None, **kwargs):
        super(SimpleTestJobDefinition, self).__init__(unexpanded_id, config=config, **kwargs)
        self.targets = targets

        self.should_run = should_run
        self.parents_should_run = parents_should_run
        self.target_type = target_type
        self.expander_type = expander_type or builder.expanders.Expander

        self.setup_dependencies_and_targets(depends_dict, targets_dict, depends, targets)


    def setup_dependencies_and_targets(self, depends_dict, targets_dict, depends, targets):
        # Set up dependency dictionary
        targets_mtime_dict = {}
        depends_dict = depends_dict or {}
        depends_dict.setdefault('depends', [])
        depends_dict.setdefault('depends_one_or_more', [])
        if depends:
            for depend in depends:
                if isinstance(depend, dict):
                    depends_type = depend.pop('type', 'depends')
                    has_mtime = "start_mtime" in depend
                    target_mtime = depend.pop('start_mtime', None)
                    expander = self.expander_type(
                            self.target_type,
                            **depend)
                    if has_mtime:
                        expander.expand = new_expand_wrapper(expander.expand,
                                                             target_mtime)
                    depends_dict[depends_type].append(expander)
                elif isinstance(depend, str):
                    depends_dict['depends'].append(
                        self.expander_type(
                            self.target_type,
                        depend)
                    )
        self.dependencies = depends_dict

        # Set up target dictionary
        targets_dict = targets_dict or {}
        targets_dict.setdefault("produces", [])
        targets_dict.setdefault("alternates", [])
        if targets:
            for target in targets:
                if isinstance(target, dict):
                    target_type = target.pop('type', 'produces')
                    has_mtime = "start_mtime" in target
                    target_mtime = target.pop('start_mtime', None)
                    expander = self.expander_type(
                        self.target_type,
                        **target
                    )
                    if has_mtime:
                        expander.expand = new_expand_wrapper(expander.expand,
                                                             target_mtime)
                    targets_dict[target_type].append(expander)
                elif isinstance(target, str):

                    targets_dict["produces"].append(
                        self.expander_type(
                            self.target_type,
                            target)
                     )
        self.targets = targets_dict


class TimestampExpandedJobDefinition(JobDefinition):
    """A job that combines the timestamp expanded node and the job node
    logic
    """
    def __init__(self, unexpanded_id=None, cache_time=None,
                 curfew="10min", file_step="5min", targets=None,
                 dependencies=None, command=None, config=None):
        super(TimestampExpandedJobDefinition, self).__init__(unexpanded_id=unexpanded_id,
                                                   cache_time=cache_time,
                                                   targets=targets,
                                                   dependencies=dependencies,
                                                   command=command,
                                                   config=config)

        self.curfew = curfew
        self.file_step = file_step

    def get_expandable_id(self):
        return self.unexpanded_id + "_%Y-%m-%d-%H-%M-%S"

    def get_job_type(self):
        return TimestampExpandedJob

    def expand(self, build_graph, build_context):
        """Expands the node based off of the file step and the start and
        end times
        """

        expanded_contexts = (builder.expanders
                                    .TimestampExpander
                                    .expand_build_context(
                                            build_context,
                                            self.get_expandable_id(),
                                            self.file_step))

        expanded_nodes = []
        for expanded_id, build_context in expanded_contexts.items():
            expanded_node = self.construct_job(expanded_id, build_graph, build_context)
            expanded_nodes.append(expanded_node)

        return expanded_nodes


class MetaTarget(object):
    """Meta targets point to jobs in the graph. Meta targets are only in rule
    dependency graphs and should never be expanded in to the build graph. When
    exapanding the graph the meta targets should simply forward the expansion to
    the next jobs.
    """
    def __init__(self, unexpanded_id="meta_target", job_collection=None,
                 config=None):
        if job_collection is None:
            job_collection = {}

        if config is None:
            config = {}

        self.unexpanded_id = unexpanded_id
        self.job_collection = job_collection
        self.config = config

    def do_get_job_collection(self):
        return self.job_collection

    def get_job_collection(self, rule_dependency_graph):
        """Returns the jobs that it should be pointing to."""
        job_collection = self.do_get_job_collection()
        enabled_job_collection = []
        for job_id in job_collection:
            if job_id in rule_dependency_graph:
                enabled_job_collection.append(job_id)
        return enabled_job_collection

    def get_enabled(self):
        """Returns whether or not the meta job should be inserted in the
        graph
        """
        return True

    def __repr__(self):
        return str(json.dumps("MetaTarget({})".format(self.job_collection), indent=2))
