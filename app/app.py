import json
import os
import re
from flask import Flask, url_for, redirect, request, make_response
from os import path
from cfde_deriva.dashboard_queries import StatsQuery, DashboardQueryHelper

app = Flask(__name__)
app.debug = True

HOSTNAME = os.getenv('DERIVA_SERVERNAME')
CATALOGID = os.getenv('DERIVA_CATALOGID')

helper = DashboardQueryHelper(hostname, catalogid)

def _error_response(err, code):
    res = make_response(err, code)
    res.headers['X-CFDE-Error'] = err
    return res

def _dcc_not_found_response(dcc_name):
    return _error_response("Named DCC not found", 404)

def _abbreviation_to_dcc(dcc_name):
    # helper.list_projects removes attributes from project and also performs an additional
    # join to compute num_subprojects
#    dccs = list(helper.list_projects(use_root_projects=True))
#    for dcc in dccs:
#        if dcc['abbreviation'] == dcc_name:
#            print("got dcc = " + str(dcc))
#            return dcc

    # direct query by DCC abbreviation with no additional joins
    p_root = helper.builder.CFDE.project_root.alias('p_root')
    p = helper.builder.CFDE.project.alias('p')
    path = p_root.link(p).filter(p.abbreviation == dcc_name)
    res = path.entities().fetch()

    if len(res) == 1:
        return res[0]
    
    return None
    
# -------------------------------------------------------------------------
# API methods for https://github.com/nih-cfde/api/blob/master/api_spec.yml
# -------------------------------------------------------------------------

# /dcc
# Returns a listing of the DCCs that have data available in the archive.
@app.route('/dcc', methods=['GET'])
def dcc_list():
    dccs = list(helper.list_projects(use_root_projects=True))
    dcc_abbrevs = [ dcc['abbreviation'] for dcc in dccs ]
    return json.dumps(dcc_abbrevs)

# /dcc/{dccName}
# Returns general information about the specified DCC, such as the Principal Investigator(s) and description.
#
#      - moniker
#      - complete_name
#      - description
#      - principal_investigators
#      - url
#      - subject_count
#      - biosample_count
#      - file_count
#      - last_updated
#
@app.route('/dcc/<string:dcc_name>', methods=['GET'])
def dcc_info(dcc_name):
    dcc = _abbreviation_to_dcc(dcc_name)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_name)

    # DCC found

    # file count
    file_count = 0
    fcounts = list(StatsQuery(helper).entity('file').dimension('data_type').dimension('project_root').fetch()),
    for fc in fcounts[0]:
        if fc['project_RID'] == dcc['RID']:
            file_count += fc['num_files']

    # subject count
    subject_count = 0
    scounts = list(StatsQuery(helper).entity('subject').dimension('data_type').dimension('project_root').fetch()),
    for sc in scounts[0]:
        if sc['project_RID'] == dcc['RID']:
            subject_count += sc['num_subjects']

    # biosample count
    biosample_count = 0
    bcounts = list(StatsQuery(helper).entity('biosample').dimension('data_type').dimension('project_root').fetch()),
    for bc in bcounts[0]:
        if bc['project_RID'] == dcc['RID']:
            biosample_count += bc['num_biosamples']

    # look for something resembling a DCC URL
    # TODO - is there a field/table that's guaranteed to contain this information?
    url = None
    for att in ['persistent_id', 'id_namespace']:
        if (dcc[att] is not None) and re.search(r'^http', dcc[att]):
            url = dcc[att]
            break
            
    return json.dumps({
        'moniker': dcc['abbreviation'],
        'complete_name': dcc['name'],
        'description': dcc['description'],
        # TODO - current schema has primary_dcc_contact, but no info on PIs
        'principal_investigators': [],
        'url': url,
        'subject_count': subject_count,
        'biosample_count': biosample_count,
        'file_count': file_count,
        'last_updated': dcc['RMT'],
    })

# /dcc/{dccName}/projects
# Returns a listing of (top-level) projects associated with the specified DCC.
@app.route('/dcc/<string:dcc_name>/projects', methods=['GET'])
def dcc_projects(dcc_name):
    dcc = _abbreviation_to_dcc(dcc_name)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_name)

    # DCC found
    projects = helper.list_projects(parent_project_RID=dcc['RID'])
    res = []
    for proj in projects:
        # TODO - nothing in project appears to be non-nullable
        name = None
        for field in ('name', 'abbreviation', 'description', 'id'):
            if proj[field] is not None:
                name = proj[field]
                break
        res.append(name)

    return json.dumps(res)

# /dcc/{dccName}/filecount
# Returns the number of files associated with a particular DCC broken down by data type.
@app.route('/dcc/<string:dcc_name>/filecount', methods=['GET'])
def dcc_filecount(dcc_name):
    dcc = _abbreviation_to_dcc(dcc_name)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_name)

    # DCC found
    fcounts = list(StatsQuery(helper).entity('file').dimension('data_type').dimension('project_root').fetch()),
    res = {}
    
    for fc in fcounts[0]:
        if fc['project_RID'] == dcc['RID']:
            res[fc['data_type_name']] = fc['num_files']

    return json.dumps(res)

# /dcc/{dccName}/stats/{variable}/{grouping}
# Returns statistics for the requested variable grouped by the specified aggregation.
@app.route('/dcc/<string:dcc_name>/stats/<string:variable>/<string:grouping>', methods=['GET'])
def dcc_grouped_stats(dcc_name,variable,grouping):
    err = None

    #  check that variable is one of 'files', 'volume', 'samples', 'subjects'
    legal_vars = ['files','volume','samples','subjects'];
    legal_vars_re_str = '^' + "|".join(legal_vars) + '$'
    legal_vars_re = re.compile('^(' + "|".join(legal_vars) + ')$')
    if not (legal_vars_re.match(variable)):
        err = "Illegal variable requested - must be one of " + ",".join(legal_vars)
        
    # check that grouping is one of 'data_type', 'assay', 'species', 'anatomy'
    legal_groups = ['data_type','assay','species','anatomy'];
    legal_groups_re = re.compile('^(' + "|".join(legal_groups) + ')$')
    if not (legal_groups_re.match(grouping)):
        err = "Illegal grouping requested - must be one of " + ",".join(legal_groups)

    # input error
    if err is not None:
        return _error_response(err, 404)

    dcc = _abbreviation_to_dcc(dcc_name)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_name)

    # parameterization
    variable_map = {
        'files': { 'entity': 'file', 'att': 'num_files' },
        'volume': { 'entity': 'file', 'att': 'num_bytes' },
        'samples': { 'entity': 'biosample','att': 'num_biosamples' },
        'subjects': { 'entity': 'subject', 'att': 'num_subjects' },
    }
    grouping_map = {
        'data_type': { 'dimension': 'data_type', 'att': 'data_type_name' },
        'assay': { 'dimension': 'assay_type', 'att': 'assay_type_name' },
        'species': { 'dimension': 'species', 'att': 'species_name' },
        'anatomy': { 'dimension': 'anatomy', 'att': 'anatomy_name' },
    }

    vm = variable_map[variable]
    gm = grouping_map[grouping]
    counts = list(StatsQuery(helper).entity(vm['entity']).dimension(gm['dimension']).dimension('project_root').fetch())   
    res = {}
    
    for ct in counts:
        if ct['project_RID'] == dcc['RID']:
            res[ct[gm['att']]] = ct[vm['att']]

    # return type is DCCGroupedStatistics, which is a list of DCCGrouping
    return json.dumps([res])
