import json
import os
import re
from flask import Flask, url_for, redirect, request, make_response
from os import path
from cfde_deriva.dashboard_queries import StatsQuery, DashboardQueryHelper

app = Flask(__name__)
app.debug = True

hostname = os.getenv('DERIVA_SERVERNAME')
catalogid = os.getenv('DERIVA_CATALOGID')

legal_vars = ['files','volume','samples','subjects'];
legal_vars_re = re.compile('^(' + "|".join(legal_vars) + ')$')

legal_groups = ['data_type','assay','species','anatomy'];
legal_groups_re = re.compile('^(' + "|".join(legal_groups) + ')$')

# same as legal groups plus 'dcc'
legal_groups_dcc = ['data_type','assay','species','anatomy','dcc'];
legal_groups_dcc_re = re.compile('^(' + "|".join(legal_groups_dcc) + ')$')

helper = DashboardQueryHelper(hostname, catalogid)

def _error_response(err, code):
    res = make_response(err, code)
    res.headers['X-CFDE-Error'] = err
    return res

def _dcc_not_found_response(dcc_name):
    return _error_response("Named DCC not found", 404)

def _abbreviation_to_dcc(dcc_name):
    # helper.list_projects removes attributes from project and also performs an additional
    # join to compute num_subprojects, which we don't need
#    dccs = list(helper.list_projects(use_root_projects=True))

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

# parameterization for dcc_grouped_stats
VARIABLE_MAP = {
    'files': { 'entity': 'file', 'att': 'num_files' },
    'volume': { 'entity': 'file', 'att': 'num_bytes' },
    'samples': { 'entity': 'biosample','att': 'num_biosamples' },
    'subjects': { 'entity': 'subject', 'att': 'num_subjects' },
}
GROUPING_MAP = {
    'data_type': { 'dimension': 'data_type', 'att': 'data_type_name' },
    'assay': { 'dimension': 'assay_type', 'att': 'assay_type_name' },
    'species': { 'dimension': 'species', 'att': 'species_name' },
    'anatomy': { 'dimension': 'anatomy', 'att': 'anatomy_name' },
    'dcc': {'dimension': 'project_root', 'att': 'project_RID' },
}

# /dcc/{dccName}/stats/{variable}/{grouping}
# Returns statistics for the requested variable grouped by the specified aggregation.
@app.route('/dcc/<string:dcc_name>/stats/<string:variable>/<string:grouping>', methods=['GET'])
def dcc_grouped_stats(dcc_name,variable,grouping):
    err = None

    #  check that variable is one of 'files', 'volume', 'samples', 'subjects'
    if not legal_vars_re.match(variable):
        err = "Illegal variable requested - must be one of " + ",".join(legal_vars)
        
    # check that grouping is one of 'data_type', 'assay', 'species', 'anatomy'
    if not legal_groups_re.match(grouping):
        err = "Illegal grouping requested - must be one of " + ",".join(legal_groups)

    # input error
    if err is not None:
        return _error_response(err, 404)

    dcc = _abbreviation_to_dcc(dcc_name)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_name)

    vm = VARIABLE_MAP[variable]
    gm = GROUPING_MAP[grouping]
    counts = list(StatsQuery(helper).entity(vm['entity']).dimension(gm['dimension']).dimension('project_root').fetch())   
    res = {}
    
    for ct in counts:
        if ct['project_RID'] == dcc['RID']:
            res[ct[gm['att']]] = ct[vm['att']]

    # return type is DCCGroupedStatistics, which is a list of DCCGrouping
    return json.dumps([res])

def _grouped_stats_aux(variable,grouping1,max_groups1,grouping2,max_groups2):

    # need to map project_RID to project_abbreviation if grouping=dcc
    rid_to_abbrev = {}
    if (grouping1 == "dcc") or (grouping2 == "dcc"):
        dccs = list(helper.list_projects(use_root_projects=True))
        for dcc in dccs:
            rid_to_abbrev[dcc['RID']] = dcc['abbreviation']
    
    vm = VARIABLE_MAP[variable]
    gm1 = GROUPING_MAP[grouping1]
    gm2 = GROUPING_MAP[grouping2]
    
    counts = list(StatsQuery(helper).entity(vm['entity']).dimension(gm1['dimension']).dimension(gm2['dimension']).fetch())   

    dim1_counts = {}
    res = []

    # StatsQuery output looks like this:
    # {'anatomy_id': 'UBERON:0002387', 'species_id': 'NCBI:txid9606', 'num_subjects': 1, 'anatomy_name': 'pes', 'species_name': 'Homo sapiens'}
    # The following code aggregates/rewrites to this format (expected by dashboard):
    # {"anatomy": "pes", "Homo sapiens": 1}
    for ct in counts:
        dim1 = ct[gm1['att']]
        dim2 = ct[gm2['att']]

        # map RID to abbreviation if needed
        if (grouping1 == "dcc"):
            dim1 = rid_to_abbrev[dim1]
        if (grouping2 == "dcc"):
            dim2 = rid_to_abbrev[dim2]
        
        if not (dim1 in dim1_counts):
            dim1_counts[dim1] = { grouping1 : dim1 }
            res.append(dim1_counts[dim1])

        dim1_counts[dim1][dim2] = ct[vm['att']]

    return res
        
# TODO - add these calls to Swagger API. Can't support dashboard without them.
# TODO - factor out parameter error-checking code

# /dcc/stats/{variable}/{grouping}
# Returns statistics for the requested variable grouped by the specified aggregation.
@app.route('/stats/<string:variable>/<string:grouping1>/<string:grouping2>', methods=['GET'])
def grouped_stats(variable,grouping1,grouping2):
    err = None

    #  check that variable is one of 'files', 'volume', 'samples', 'subjects'
    if not legal_vars_re.match(variable):
        err = "Illegal variable requested - must be one of " + ",".join(legal_vars)
    # check that grouping1 and grouping2 is one of 'data_type', 'assay', 'species', 'anatomy', 'dcc'
    if not legal_groups_dcc_re.match(grouping1):
        err = "Illegal grouping1 requested - must be one of " + ",".join(legal_groups)
    if not legal_groups_dcc_re.match(grouping2):
        err = "Illegal grouping2 requested - must be one of " + ",".join(legal_groups)
    if grouping1 == grouping2:
        err = "grouping1 and grouping2 cannot be the same dimension."
    
    # input error
    if err is not None:
        return _error_response(err, 404)

    res = _grouped_stats_aux(variable, grouping1, None, grouping2, None)

    # return type is DCCGroupedStatistics, which is a list of DCCGrouping
    return json.dumps(res)

def _merge_within_groups(groups, max_atts):
    # add up group2 counts across all DCCGroupings
    gcounts = {}

    for group in groups:
        for k in group:
            if not legal_groups_dcc_re.match(k):
                if k not in gcounts:
                    gcounts[k] = 0
                gcounts[k] += group[k]

    # sort groups and determine which to merge
    gsorted = sorted(list(gcounts.keys()), key=lambda x: gcounts[x], reverse=True)

    if len(gsorted) <= max_atts:
        return groups

    # create mapping from old group to new group
    gmap = {}
    i = 0
    for group in gsorted:
        if i >= max_atts:
            gmap[group] = 'other'
        else:
            gmap[group] = group
        i += 1

    # apply mapping to groups
    new_groups = []
    for group in groups:
        new_group = {}
        for k in group:
            new_k = k
            if k in gmap:
                new_k = gmap[k]
            if new_k in new_group:
                new_group[new_k] += group[k]
            else:
                new_group[new_k] = group[k]

        new_groups.append(new_group)
    
    return new_groups

# returns groups sorted by descending total count, even if len(groups) <= max_groups
def _merge_groups(groups, max_groups):
    # sort groups by total count, retain the max_groups with the highest counts
    groups_w_count = []
    for group in groups:
        gwc = { 'group': group, 'total': 0}
        groups_w_count.append(gwc)
        for k in group:
            if not legal_groups_dcc_re.match(k):
                gwc['total'] += group[k]

    sorted_gwc = sorted(groups_w_count, key=lambda x: x['total'], reverse=True)
    sorted_groups = [x['group'] for x in sorted_gwc]
    
    new_groups = []
    last_group = {}
    i = 0

    for group in sorted_groups:
        # add group to list
        if i < max_groups:
            new_groups.append(group)
        # add group to last group
        else:
            for k in group:
                if re.match(r'^(anatomy|data_type|assay_type|species|dcc)$', k):
                    last_group[k] = 'other'
                else:
                    if k in last_group:
                        last_group[k] += group[k]
                    else:
                        last_group[k] = group[k]
        i += 1

    if len(last_group.keys()) > 0:
        new_groups.append(last_group)
    return new_groups
    
# /dcc/stats/{variable}/{grouping}
# Returns statistics for the requested variable grouped by the specified aggregation.
# If there are more than maxgroups1 groups in grouping1 or more than maxgroups2 groups
# in grouping2 then the extra groups will be merged into a single additional group
# called 'other'.
@app.route('/stats/<string:variable>/<string:grouping1>/<int:maxgroups1>/<string:grouping2>/<int:maxgroups2>', methods=['GET'])
def grouped_stats_other(variable,grouping1,maxgroups1,grouping2,maxgroups2):
    err = None

    #  check that variable is one of 'files', 'volume', 'samples', 'subjects'
    if not legal_vars_re.match(variable):
        err = "Illegal variable requested - must be one of " + ",".join(legal_vars)
        
    # check that grouping1 and grouping2 is one of 'data_type', 'assay', 'species', 'anatomy', 'dcc'
    if not legal_groups_dcc_re.match(grouping1):
        err = "Illegal grouping1 requested - must be one of " + ",".join(legal_groups)
    if not legal_groups_dcc_re.match(grouping2):
        err = "Illegal grouping2 requested - must be one of " + ",".join(legal_groups)
    if grouping1 == grouping2:
        err = "grouping1 and grouping2 cannot be the same dimension."

    if (maxgroups1 is not None) and (maxgroups1 < 0):
        err = "maxgroups1 must be >= 0"
    if (maxgroups2 is not None) and (maxgroups2 < 0):
        err = "maxgroups2 must be >= 0"
        
    # input error
    if err is not None:
        return _error_response(err, 404)

    # returns list of DCCGrouping
    res = _grouped_stats_aux(variable, grouping1, maxgroups1, grouping2, maxgroups2)

    # merge groups2 (i.e., merge counts within each DCCGrouping)
    if maxgroups2 is not None:
        res = _merge_within_groups(res, maxgroups2)
        
    # merge groups1 (i.e., merge GCCGroupings
    if maxgroups1 is not None:
        res = _merge_groups(res, maxgroups1)
    
    # return type is DCCGroupedStatistics, which is a list of DCCGrouping
    return json.dumps(res)
