import logging
import os
import string
import time
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from httpmr import base
from httpmr import driver
from httpmr import sinks
from wsgiref import handlers


class Error(Exception): pass
class UnknownTaskError(Error): pass
class MissingRequiredParameterError(Error): pass


# Some constants for URL handling.  These values must correspond to the base
# name of the template that should be rendered at task completion.  For
# instance, when a mapper task is completed, the name of the template that will
# be rendered is MAPPER_TASK_NAME + ".html"
MAP_MASTER_TASK_NAME = driver.MAP_MASTER_TASK_NAME
MAPPER_TASK_NAME = "mapper"
REDUCE_MASTER_TASK_NAME = driver.REDUCE_MASTER_TASK_NAME
REDUCER_TASK_NAME = "reducer"
INTERMEDIATE_DATA_CLEANUP_MASTER_TASK_NAME = \
    driver.INTERMEDIATE_DATA_CLEANUP_MASTER_TASK_NAME
INTERMEDIATE_DATA_CLEANUP_TASK_NAME = "cleanup"
VALID_TASK_NAMES = [MAP_MASTER_TASK_NAME,
                    MAPPER_TASK_NAME,
                    REDUCE_MASTER_TASK_NAME,
                    REDUCER_TASK_NAME,
                    INTERMEDIATE_DATA_CLEANUP_MASTER_TASK_NAME,
                    INTERMEDIATE_DATA_CLEANUP_TASK_NAME]

SOURCE_START_POINT = "source_start_point"
SOURCE_END_POINT = "source_end_point"
SOURCE_MAX_ENTRIES = "source_max_entries"
GREATEST_UNICODE_CHARACTER = "\xEF\xBF\xBD"


def tobase(base, number):
  """Ugly.
  
  I really wish I didn't have to copy this over, why doesn't Python have a
  built-in function for representing an int as a string in an arbitrary base?
  
  Copied from:
    http://www.megasolutions.net/python/How-to-convert-a-number-to-binary_-78436.aspx
  """
  number = int(number) 
  base = int(base)         
  if base < 2 or base > 36: 
    raise ValueError, "Base must be between 2 and 36"     
  if not number: 
    return 0
  symbols = string.digits + string.lowercase[:26] 
  answer = [] 
  while number: 
    number, remainder = divmod(number, base) 
    answer.append(symbols[remainder])       
  return ''.join(reversed(answer)) 

def tob36(number):
  return tobase(36, number)


class TaskSetTimer(object):
  
  def __init__(self, timeout_sec=10.0):
    self.timeout_sec = timeout_sec
    self.task_completion_times = []
  
  def Start(self):
    self.start_time = time.time()
  
  def TaskCompleted(self):
    self.task_completion_times.append(time.time())
  
  def ShouldStop(self):
    if len(self.task_completion_times) == 0:
      return False
    max_execution_time = 0
    for i in xrange(len(self.task_completion_times)):
      if i == 0: continue
      start_time = self.task_completion_times[i-1]
      end_time = self.task_completion_times[i]
      max_execution_time = max(max_execution_time,
                               end_time - start_time)
    worst_case_completion_time = time.time() + max_execution_time
    worst_case_completion_time_since_start_time = \
        worst_case_completion_time - self.start_time
    return (worst_case_completion_time_since_start_time >
            self.timeout_sec * 0.8)


class OperationStatistics(object):
  
  READ = "read"
  WRITE = "write"
  MAP = "map"
  REDUCE = "reduce"
  _valid_operation_names = [READ, WRITE, MAP, REDUCE]
  
  def __init__(self):
    self._operation_timing = {}
    for name in self._valid_operation_names:
      self._operation_timing[name] = 0
    self._started = False
  
  def Start(self, operation):
    assert not self._started
    assert operation in self._valid_operation_names
    self._started = True
    self._operation = operation
    self._last_operation_time = time.time()
  
  def _Increment(self, name):
    original = self._operation_timing[name]
    self._operation_timing[name] = \
        original + time.time() - self._last_operation_time
  
  def Stop(self):
    assert self._started
    self._started = False
    self._Increment(self._operation)
  
  def GetStatistics(self):
    lines = []
    for key in self._operation_timing:
      lines.append("%s %s" % (key, self._operation_timing[key]))
    return "\n".join(lines)


class Master(webapp.RequestHandler):
  """The MapReduce master coordinates mappers, reducers, and data."""
  
  def QuickInit(self,
                jobname,
                mapper=None,
                reducer=None,
                source=None,
                mapper_sink=None,
                reducer_source=None,
                sink=None):
    logging.debug("Beginning QuickInit.")
    assert jobname is not None
    self._jobname = jobname
    self.SetMapper(mapper)
    self.SetReducer(reducer)
    self.SetSource(source)
    self.SetMapperSink(mapper_sink)
    self.SetReducerSource(reducer_source)
    self.SetSink(sink)
    logging.debug("Done QuickInit.")
    return self
  
  def SetMapper(self, mapper):
    """Set the Mapper that should be used for mapping operations."""
    assert isinstance(mapper, base.Mapper)
    self._mapper = mapper
    return self
  
  def SetReducer(self, reducer):
    """Set the Reducer that should be used for reduce operations."""
    assert isinstance(reducer, base.Reducer)
    self._reducer = reducer
    return self
  
  def SetCleanupMapper(self, cleanup_mapper):
    """Set the Mapper that should be used to clean up the intermediate data.
    
    Sets a Mapper that will clean up the intermediate data created by the
    primary Mapper class.  This Mapper's source will be the same as the
    Reducer's source.
    """
    assert isinstance(cleanup_mapper, base.Mapper)
    self._cleanup_mapper = cleanup_mapper
    return self
    
  def SetSource(self, source):
    """Set the data source from which mapper input should be read."""
    self._source = source
    return self
  
  def SetMapperSink(self, sink):
    """Set the data sink to which mapper output should be written."""
    self._mapper_sink = sink
    return self
  
  def SetReducerSource(self, source):
    """Set the data source from which reducer input should be read."""
    self._reducer_source = source
    return self
  
  def SetSink(self, sink):
    """Set the data sink to which reducer output should be written."""
    self._sink = sink
    return self
  
  def get(self):
    """Handle task dispatch."""
    logging.debug("MapReduce Master Dispatching Request.")

    task = None
    try:
      task = self.request.params["task"]
    except KeyError, e:
      pass
    if task is None:
      task = MAP_MASTER_TASK_NAME
    
    template_data = {}
    if task == MAP_MASTER_TASK_NAME:
      template_data = self.GetMapMaster()
    elif task == MAPPER_TASK_NAME:
      template_data = self.GetMapper()
    elif task == REDUCE_MASTER_TASK_NAME:
      template_data = self.GetReduceMaster()
    elif task == REDUCER_TASK_NAME:
      template_data = self.GetReducer()
    elif task == INTERMEDIATE_DATA_CLEANUP_MASTER_TASK_NAME:
      template_data = self.GetCleanupMaster()
    elif task == INTERMEDIATE_DATA_CLEANUP_TASK_NAME:
      template_data = self.GetCleanupMapper()
    else:
      raise UnknownTaskError("Task name '%s' is not recognized.  Valid task "
                             "values are %s" % (task, VALID_TASK_NAMES))
    self.RenderResponse("%s.html" % task, template_data)
  
  def _NextUrl(self, path_data):
    logging.debug("Rendering next url with path data %s" % path_data)
    path = self.request.path_url
    path_data["path"] = path
    return ("%(path)s?task=%(task)s"
            "&source_start_point=%(source_start_point)s"
            "&source_end_point=%(source_end_point)s"
            "&source_max_entries=%(source_max_entries)d") % path_data
  
  def _GetShardBoundaries(self):
    # TODO(peterdolan): Expand this to allow an arbitrary number of shards
    # instead of a fixed set of 36 shards.
    boundaries = [""]
    for i in xrange(35):
      j = (i + 1)
      boundaries.append(tob36(j))
    boundaries.append(GREATEST_UNICODE_CHARACTER)
    return boundaries
  
  def _GetShardBoundaryTuples(self):
    boundaries = self._GetShardBoundaries()
    boundary_tuples = []
    for i in xrange(len(boundaries)):
      if i == 0:
        continue
      boundary_tuples.append((boundaries[i-1], boundaries[i]))
    return boundary_tuples
  
  def _GetUrlsForShards(self, task):
    urls = []
    for boundary_tuple in self._GetShardBoundaryTuples():
      start_point = boundary_tuple[0]
      end_point = boundary_tuple[1]
      urls.append(self._NextUrl({"task": task,
                                 SOURCE_START_POINT: start_point,
                                 SOURCE_END_POINT: end_point,
                                 SOURCE_MAX_ENTRIES: 1000}))
    return urls
  
  def GetMapMaster(self):
    """Handle Map controlling page."""
    return {'urls': self._GetUrlsForShards(MAPPER_TASK_NAME)}

  def GetMapper(self):
    """Handle mapper tasks."""
    return self._GetGeneralMapper(self._mapper, self._source, self._mapper_sink)
  
  def _GetGeneralMapper(self, mapper, source, sink):
    """Handle general Mapper tasks.
    
    specifically base mapping and intermediate data cleanup.
    """
    # Initialize the statistics object, to time the operations for reporting
    statistics = OperationStatistics()

    # Grab the parameters for this map task from the URL
    start_point = self.request.params[SOURCE_START_POINT]
    end_point = self.request.params[SOURCE_END_POINT]
    max_entries = int(self.request.params[SOURCE_MAX_ENTRIES])
    
    statistics.Start(OperationStatistics.READ)
    mapper_data = source.Get(start_point, end_point, max_entries)
    statistics.Stop()
    
    # Initialize the timer, and begin timing our operations
    timer = TaskSetTimer()
    timer.Start()
    
    last_key_mapped = None
    values_mapped = 0

    statistics.Start(OperationStatistics.READ)
    for key_value_pair in mapper_data:
      statistics.Stop()
      if timer.ShouldStop():
        break
      key = key_value_pair[0]
      value = key_value_pair[1]
      statistics.Start(OperationStatistics.MAP)
      for output_key_value_pair in mapper.Map(key, value):
        statistics.Stop()
        output_key = output_key_value_pair[0]
        output_value = output_key_value_pair[1]
        
        statistics.Start(OperationStatistics.WRITE)
        sink.Put(output_key, output_value)
        statistics.Stop()
        
        statistics.Start(OperationStatistics.MAP)
      statistics.Stop()
      last_key_mapped = key
      values_mapped += 1
      timer.TaskCompleted()
      statistics.Start(OperationStatistics.READ)
    
    next_url = None
    if values_mapped > 0:
      logging.debug("Completed %d map operations" % values_mapped)
      next_url = self._NextUrl({"task": MAPPER_TASK_NAME,
                                SOURCE_START_POINT: last_key_mapped,
                                SOURCE_END_POINT: end_point,
                                SOURCE_MAX_ENTRIES: max_entries})
    else:
      next_url = None
    return { "next_url": next_url,
             "statistics": statistics.GetStatistics() }
      
  def GetReduceMaster(self):
    """Handle Reduce controlling page."""
    return {'urls': self._GetUrlsForShards(REDUCER_TASK_NAME)}

  def GetReducer(self):
    """Handle reducer tasks."""
    statistics = OperationStatistics()
    
    # Grab the parameters for this map task from the URL
    start_point = self.request.params[SOURCE_START_POINT]
    end_point = self.request.params[SOURCE_END_POINT]
    max_entries = int(self.request.params[SOURCE_MAX_ENTRIES])
    
    reducer_keys_values = self._GetReducerKeyValues(start_point,
                                                    end_point,
                                                    max_entries,
                                                    statistics)
    
    last_key_reduced = None
    keys_reduced = 0
    # Initialize the timer, and begin timing our operations
    timer = TaskSetTimer()
    timer.Start()
    for key in reducer_keys_values:
      if timer.ShouldStop():
        break
      values = reducer_keys_values[key]
      statistics.Start(OperationStatistics.REDUCE)
      for output_key_value_pair in self._reducer.Reduce(key, values):
        statistics.Stop()
        
        output_key = output_key_value_pair[0]
        output_value = output_key_value_pair[1]
        
        statistics.Start(OperationStatistics.WRITE)
        self._sink.Put(output_key, output_value)
        statistics.Stop()
        
        statistics.Start(OperationStatistics.REDUCE)
      statistics.Stop()
      last_key_reduced = key
      keys_reduced += 1
      timer.TaskCompleted()
    
    next_url = None
    if keys_reduced > 0:
      logging.debug("Completed %d reduce operations" % keys_reduced)
      next_url = self._NextUrl({"task": REDUCER_TASK_NAME,
                                SOURCE_START_POINT: last_key_reduced,
                                SOURCE_END_POINT: end_point,
                                SOURCE_MAX_ENTRIES: max_entries})
    else:
      next_url = None
    return { "next_url": next_url,
             "statistics": statistics.GetStatistics() }
  
  def _GetReducerKeyValues(self,
                           start_point,
                           end_point,
                           max_entries,
                           statistics):
    statistics.Start(OperationStatistics.READ)
    reducer_data = self._reducer_source.Get(start_point, end_point, max_entries)
    statistics.Stop()
    
    # Retrieve the mapped data from the datastore and sort it by key.
    #
    # The Source interface specification guarantees that we will retrieve every
    # intermediate value for a given key.
    reducer_keys_values = {}
    statistics.Start(OperationStatistics.READ)
    for key_value_pair in reducer_data:
      key = key_value_pair[0]
      value = key_value_pair[1].intermediate_value
      if key in reducer_keys_values:
        reducer_keys_values[key].append(value)
      else:
        reducer_keys_values[key] = [value]
    statistics.Stop()
    return reducer_keys_values
  
    
  def GetCleanupMaster(self):
    """Handle Cleanup controlling page."""
    return {'urls': self._GetUrlsForShards(INTERMEDIATE_DATA_CLEANUP_TASK_NAME)}
  
  def GetCleanupMapper(self):
    """Handle Cleanup Mapper tasks."""
    self._GetGeneralMapper(self._cleanup_mapper,
                           self._reducer_source,
                           sinks.NoOpSink())
  
  def RenderResponse(self, template_name, template_data):
    path = os.path.join(os.path.dirname(__file__),
                        'templates',
                        template_name)
    logging.debug("Rendering template at path %s" % path)
    self.response.out.write(template.render(path, template_data))