from dlt.extract.resource import DltResource
import dlt as dlt
from rest_api import RESTAPIConfig, rest_api_resources
from dagster_embedded_elt.dlt import DagsterDltResource, dlt_assets, DagsterDltTranslator
from dagster import AssetExecutionContext, AssetKey, Definitions, DailyPartitionsDefinition, schedule, asset, asset_check, define_asset_job, BackfillPolicy, AssetCheckResult, AssetIn, AssetSelection, RunRequest
from dagster_duckdb_pandas import DuckDBPandasIOManager
import pandas as pd
import datetime


# The first section here uses regular dlt conventions to define a REST API as a source
# and a pipeline that will load the data from that api to a duckdb warehouse
# The API is incremental, loading only new data
# See the dlt docs for more information

@dlt.source
def students_raw():
    config: RESTAPIConfig = {
        "client": {
            "base_url": "http://localhost:4545/api/"
        },
        "resource_defaults": {
            "primary_key": "id",
            "write_disposition": "merge",
        },
        "resources": [
            {
                "name": "data",
                "endpoint": {
                    "path": "data",
                    "params": {
                        "created_at": {
                            "type": "incremental",
                            "cursor_path": "created_at",
                            "initial_value": "2024-06-01T12:00:00Z",
                        },
                    },
                }
            }
        ]
    }

    yield from rest_api_resources(config)

pipeline = dlt.pipeline(
    pipeline_name="load_student_data",
    destination="duckdb",
    dataset_name="students_raw"
)


# The next section represents the dlt pipeline as a set of
# dagster assets that can be invoked

class DuckDbDltTranslator(DagsterDltTranslator):
    def get_asset_key(self, resource: DltResource) -> AssetKey:
        """ Determine how to name the assets in dagster that are created by the dlt pipeline"""

        # this particular choice is necessary so that the DuckDB IO Manager used later can 
        # appropriately load the table generated by dlt into a pandas data frame input
        return AssetKey(["students_raw", str(resource.table_name)])

@dlt_assets(
    dlt_source=students_raw(),
    dlt_pipeline=pipeline,
    dlt_dagster_translator=DuckDbDltTranslator(),
    name="students_raw"
)
def load_dlt_students(context: AssetExecutionContext, dlt: DagsterDltResource):
    """ Incrementally loads new data from the student API into the duck db warehouse """
    yield from dlt.run(context=context)


# This section creates some assets downstream of the loaded data

@asset(
        # tell dagster we want the asset to be materialized using the warehouse (duckdb) io manager, see the resources part of the Definitions object
        io_manager_key="warehouse",
        # because the dlt integration does not use an IO manager, we manually specify how to load the table dlt created as a data frame 
        ins={"students_raw_data": AssetIn(key=["students_raw", "data"], input_manager_key="warehouse")},
        # tell dagster this asset is partitioned, meaning we want to drop and replace only one partition during normal runs of the asset
        partitions_def=DailyPartitionsDefinition(start_date="2024-06-01"),
        # tell dagster when we go to drop a partition, which column in the table represents the partition, this is passed to the IO manager
        metadata={"partition_expr": "date"},
        # however if we do want to operate on all partitions, eg during a backfill, do so in one run that drops the whole table and updates it all at once
        backfill_policy=BackfillPolicy.single_run(),
        # these final two fields just adjust the UI and data catalog, they don't affect computation
        compute_kind="pandas",
        tags={"dagster/storage_kind": "duckdb"},
) 
def students_summary_by_day(context: AssetExecutionContext, students_raw_data: pd.DataFrame):
    """ A table of summary statistics for the students. Everytime this runs, the current hourly partition is dropped and re-written. In a backfill, all partitions can be dropped and re-run. """
    
    # if you want to access the partition(s) being operated on by the current run
    context.partition_key_range

    students_raw_data['date'] = students_raw_data['created_at'].dt.floor('D')
    counts = students_raw_data.groupby(['student', 'date']).size().reset_index(name='count')

    # because counts is a pandas dataframe being written using an io manager, a lot of metadata is logged for free
    return counts

@asset_check(
    asset=students_summary_by_day, 
)
def check_expected_student_values(students_summary_by_day: pd.DataFrame): 
    """ A data quality check applied ot the student summary by day table """
    unique_students = students_summary_by_day['student'].unique()
    return AssetCheckResult(
        passed= set(unique_students) == set(['a', 'b', 'c']),
        metadata={"students_in_dataset": str(unique_students)}
    )

@asset(
    # for this asset we only need to specify the io manager used to store the output
    # the input will be loaded automatically using the upstream's io manager 
    io_manager_key="warehouse",
    compute_kind="pandas",
    tags={"dagster/storage_kind": "duckdb"}, 
)
def students_summary(students_summary_by_day: pd.DataFrame): 
    """ Non-partitioned asset that is updated any time new data arrives upstream by dropping the whole table and recalculating it """
    return students_summary_by_day.groupby('student')['count'].sum().reset_index()

# Now we can create a job to run our pipeline 
# There are a few ways to run assets in Dagster (by schedule, by event, or automatically to propagate change)
# In this example we'll create a simple schedule that runs our dlt pipeline every hour, and then re-calculates our current daily partition
# (dropping the data for that day so far, than re-calculating it)
# See https://github.com/dagster-io/dagster/discussions/18893 
# In many cases you can simplify things by just aligning your schedule to your partition definition see: https://docs.dagster.io/concepts/automation/schedules/partitioned-schedules#asset-jobs

run_load = define_asset_job(
    name="run_load",
    selection=AssetSelection.assets(students_summary_by_day).upstream(),
)

@schedule(
    cron_schedule="0 * * * *",
    job=run_load, 
)
def run_load_hourly():
    current_date = datetime.datetime.now()
    current_day_string = current_date.strftime('%Y-%m-%d')
    RunRequest(
        partition_key=current_day_string
    )    


# Run our final summary job once a day
run_summary=define_asset_job(
    name="run_summary", 
    selection=AssetSelection.assets(students_summary)
)

@schedule(
    cron_schedule="@daily",
    job=run_summary
)
def run_summary_daily():
    RunRequest()

# Finally we wrap everything together in the dagster Definitions object

defs = Definitions(
    assets=[load_dlt_students, students_summary, students_summary_by_day],
    asset_checks=[check_expected_student_values],
    schedules=[run_load_hourly, run_summary_daily],
    resources={"dlt": DagsterDltResource(), "warehouse": DuckDBPandasIOManager(database="load_student_data.duckdb")}
)
