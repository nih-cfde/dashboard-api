import json
import os
import re
import requests.exceptions
import sys
from flask import Flask, url_for, redirect, request, make_response, wrappers
from flask_cors import CORS, cross_origin
from os import path
from cfde_deriva.dashboard_queries import StatsQuery, DashboardQueryHelper
from deriva.core.datapath import Min, Max, Cnt, CntD, Avg, Sum, Bin

# allowed CORS origins
origin_regex_str = os.getenv('CORS_ORIGIN_REGEX')
if origin_regex_str is None:
    origin_regex_str = 'http://localhost'
origin_regex = re.compile(origin_regex_str)

app = Flask(__name__)
cors = CORS(app, resources={r'.*': {'origins': origin_regex}})
app.debug = True
show_nulls = True

hostname = os.getenv('DERIVA_SERVERNAME')
default_catalog_id = os.getenv('DERIVA_DEFAULT_CATALOGID')

legal_vars = ['files','volume','samples','subjects'];
legal_vars_re = re.compile('^(' + "|".join(legal_vars) + ')$')

legal_groups = ['data_type','assay','species','anatomy'];
legal_groups_re = re.compile('^(' + "|".join(legal_groups) + ')$')

# same as legal groups plus 'dcc'
legal_groups_dcc = ['data_type','assay','species','anatomy','dcc'];
legal_groups_dcc_re = re.compile('^(' + "|".join(legal_groups_dcc) + ')$')

helpers = {}

def _error_response(err, code):
    res = make_response(err, code)
    res.headers['X-CFDE-Error'] = err
    return res

def _catalog_not_found_response(catalog_id):
    return _error_response("DERIVA catalogId " + str(catalog_id) + " not found", 404)

def _dcc_not_found_response(dcc_name):
    return _error_response("DCC '" + dcc_name + "' not found", 404)

# Retrieve DashboardQueryHelper for the specified catalogid, using default catalogid if None.
#
def _get_helper(catalog_id):
    if catalog_id is None:
        catalog_id = default_catalog_id

    if catalog_id not in helpers:
        err = None
        try:
            helpers[catalog_id] = DashboardQueryHelper(hostname, catalog_id)
        # invalid catalog id
        except requests.exceptions.HTTPError as e:
            err = e
            # don't cache the result, because the catalog in question could be created in future
            if re.match(r'.*The requested catalog \S+ could not be found.*', str(e.response.content)):
                return _catalog_not_found_response(catalog_id)    
        except Exception as e:
            err = e

        # all other cfde-deriva errors
        if err is not None:
            return _error_response(str(err), 500)
        
    return helpers[catalog_id]

def _abbreviation_to_dcc(helper, dcc_name):
    # helper.list_projects removes attributes from project and also performs an additional
    # join to compute num_subprojects, which we don't need, so we perform a
    # direct query by DCC abbreviation with no additional joins
    p_root = helper.builder.CFDE.project_root.alias('p_root')
    p = helper.builder.CFDE.project.alias('p')
    path = p_root.link(p).filter(p.abbreviation == dcc_name)
    res = path.entities().fetch()

    if len(res) == 1:
        return res[0]
    
    return None

def _all_dccs(helper):
    p_root = helper.builder.CFDE.project_root.alias('p_root')
    p = helper.builder.CFDE.project.alias('p')
    res = p_root.link(p).entities().fetch()
    return res

# -------------------------------------------------------------------------
# API methods for https://github.com/nih-cfde/api/blob/master/api_spec.yml
# -------------------------------------------------------------------------

# /dcc
# Returns a listing of the DCCs that have data available in the archive.
@app.route('/dcc', methods=['GET'])
def dcc_list():
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper
    
    dccs = list(helper.list_projects(use_root_projects=True))
    dcc_abbrevs = [ dcc['abbreviation'] for dcc in dccs ]
    return json.dumps(dcc_abbrevs)

# /dcc_info
# Returns summary info for all DCCs in the archive, similar to /dcc/{dccName} for
# a single DCC.
@app.route('/dcc_info', methods=['GET'])
def all_dcc_info():
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    num_subjects = 0
    num_biosamples = 0
    num_files = 0
    num_projects = 0
    last_updated = None

    # subject, file, biosample, and project counts
    counts = _get_dcc_entity_counts(helper, None, { 'subject': True, 'file': True, 'biosample': True, 'project': True })
    # all DCCs
    dccs = _all_dccs(helper)
    
    for dcc in dccs:
        if last_updated is None:
            last_updated = dcc['RMT']
        if dcc['RMT'] > last_updated:
            last_updated = dcc['RMT']
        sys.stderr.write("RMT=" + str(dcc['RMT']) + " last_updated=" + str(last_updated) + "\n")

    return json.dumps({
        'subject_count': counts['subject_count'],
        'biosample_count': counts['biosample_count'],
        'file_count': counts['file_count'],
        'project_count': counts['project_count'],
        'last_updated': last_updated,
    })

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
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    dcc = _abbreviation_to_dcc(helper, dcc_name)
    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_name)

    # DCC found

    # subject, file, and biosample counts
    counts = _get_dcc_entity_counts(helper, dcc_name, { 'subject': True, 'file': True, 'biosample': True })

    dcc_url = None
    
    # primary DCC contact
    p_root = helper.builder.CFDE.project_root.alias('p_root')
    p = helper.builder.CFDE.project.alias('p')
    path = p_root.link(p).filter(p.abbreviation == dcc_name)
    pdc = helper.builder.CFDE.primary_dcc_contact.alias('pdc')
    path = path.link(pdc)
    res = path.entities().fetch()

    # TODO - include dcc contact info in summary API endpoint?
    if len(res) == 1:
        dcc_url = res[0]['dcc_url']
        
    # otherwise look for something resembling a DCC URL in the project
    if dcc_url is None:
        for att in ['persistent_id', 'id_namespace']:
            if (dcc[att] is not None) and re.search(r'^http', dcc[att]):
                dcc_url = dcc[att]
                break
            
    return json.dumps({
        'moniker': dcc['abbreviation'],
        'complete_name': dcc['name'],
        'description': dcc['description'],
        # TODO - current schema has primary_dcc_contact, but no info on PIs
        'principal_investigators': [],
        'url': dcc_url,
        'subject_count': counts['subject_count'],
        'biosample_count': counts['biosample_count'],
        'file_count': counts['file_count'],
        'last_updated': dcc['RMT'],
    })

# /dcc/{dccName}/projects
# Returns a listing of (top-level) projects associated with the specified DCC.
@app.route('/dcc/<string:dcc_name>/projects', methods=['GET'])
def dcc_projects(dcc_name):
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper
    
    dcc = _abbreviation_to_dcc(helper, dcc_name)

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

# retrieve entity count from StatsQuery response, replacing 'null' keys with 'unknown'
# and null counts with zero
def _get_stats_name_and_count(row, key_att, count_att):
    name = row[key_att]
    count = row[count_att]
    if (name is None) or (name == 'null'):
        name = 'unknown'
    if count is None:
        count = 0
    return {'name': name, 'count': count}
        
# /dcc/{dccName}/filecount
# Returns the number of files associated with a particular DCC broken down by data type.
@app.route('/dcc/<string:dcc_name>/filecount', methods=['GET'])
def dcc_filecount(dcc_name):
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper
    
    dcc = _abbreviation_to_dcc(helper, dcc_name)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_name)

    # DCC found
    fcounts = list(StatsQuery(helper).entity('file').dimension('data_type',show_nulls=show_nulls).dimension('project_root',show_nulls=show_nulls).fetch())
    res = {}

    for fc in fcounts:
        if fc['project_RID'] == dcc['RID']:
            nc = _get_stats_name_and_count(fc, 'data_type_name', 'num_files')
            res[nc['name']] = nc['count']

    return json.dumps(res)

# don't filter by DCC if dcc_name is None
def _get_dcc_entity_counts(helper, dcc_name, counts):
    res = {}
    
    # get path to all subprojects of DCC
    def get_proj_path():
        p_root = helper.builder.CFDE.project_root.alias('p_root')
        p = helper.builder.CFDE.project.alias('p')
        pipt = helper.builder.CFDE.project_in_project_transitive.alias('pipt')
        if dcc_name is None:
            path = p_root.link(p)
        else:
            path = p_root.link(p).filter(p.abbreviation == dcc_name)
        proj_path = path.link(pipt, on= ((p_root.project_id_namespace == pipt.leader_project_id_namespace)
                                    & (p_root.project_local_id == pipt.leader_project_local_id )))
        return proj_path

    # get path to only top-level subprojects of a root project (i.e., DCC)
    def get_subproj_path():
        p_root = helper.builder.CFDE.project_root.alias('p_root')
        p = helper.builder.CFDE.project.alias('p')
        pip = helper.builder.CFDE.project_in_project.alias('pip')
        if dcc_name is None:
            path = p_root.link(p)
        else:
            path = p_root.link(p).filter(p.abbreviation == dcc_name)
        proj_path = path.link(pip, on= ((p_root.project_id_namespace == pip.parent_project_id_namespace)
                                    & (p_root.project_local_id == pip.parent_project_local_id )))
        return proj_path

    # path to DCCs' subjects
    def get_subj_path():
        proj_path = get_proj_path()
        s = helper.builder.CFDE.subject.alias('s')
        subj_path = proj_path.link(s, on= ((proj_path.pipt.member_project_id_namespace == s.project_id_namespace)
                                           & (proj_path.pipt.member_project_local_id == s.project_local_id )))
        return subj_path

    # path to DCCs' biosamples
    def get_biosample_path():
        proj_path = get_proj_path()
        b = helper.builder.CFDE.biosample.alias('b')
        biosample_path = proj_path.link(b, on= ((proj_path.pipt.member_project_id_namespace == b.project_id_namespace)
                                                & (proj_path.pipt.member_project_local_id == b.project_local_id )))
        return biosample_path

    # path to DCCs' files
    def get_file_path():
        proj_path = get_proj_path()
        f = helper.builder.CFDE.file.alias('f')
        file_path = proj_path.link(f, on= ((proj_path.pipt.member_project_id_namespace == f.project_id_namespace)
                                           & (proj_path.pipt.member_project_local_id == f.project_local_id )))
        return file_path

    # subproject count
    if (counts is None) or ('project' in counts):
        sp = get_subproj_path()
        qr = sp.aggregates(CntD(sp.pip.RID).alias('num_projects')).fetch()
        res['project_count'] = qr[0]['num_projects']

    # subject count
    if (counts is None) or ('subject' in counts):
        sp = get_subj_path()
        qr = sp.aggregates(CntD(sp.s.RID).alias('num_subjects')).fetch()
        res['subject_count'] = qr[0]['num_subjects']
    
    # subjects linked to biosamples
    if (counts is None) or ('subject_with_biosample' in counts):
        bs = helper.builder.CFDE.biosample_from_subject
        sp = get_subj_path().link(bs)
        qr = sp.aggregates(CntD(sp.s.RID).alias('num_subjects_with_biosamples')).fetch()
        res['subject_with_biosample_count'] = qr[0]['num_subjects_with_biosamples']

    # subjects linked to files
    if (counts is None) or ('subject_with_file' in counts):
        fs = helper.builder.CFDE.file_describes_subject
        sp = get_subj_path().link(fs)
        qr = sp.aggregates(CntD(sp.s.RID).alias('num_subjects_with_files')).fetch()
        res['subject_with_file_count'] = qr[0]['num_subjects_with_files']

    # biosample count
    if (counts is None) or ('biosample' in counts):
        bp = get_biosample_path()
        qr = bp.aggregates(CntD(bp.b.RID).alias('num_biosamples')).fetch()
        res['biosample_count'] = qr[0]['num_biosamples']
        
    # biosamples linked to subjects
    if (counts is None) or ('biosample_with_subject' in counts):
        bp = get_biosample_path().link(bs)
        qr = bp.aggregates(CntD(bp.b.RID).alias('num_biosamples_with_subjects')).fetch()
        res['biosample_with_subject_count'] = qr[0]['num_biosamples_with_subjects']

    # biosamples about which files were produced
    if (counts is None) or ('biosample_with_file' in counts):
        fb = helper.builder.CFDE.file_describes_biosample
        bp = get_biosample_path().link(fb)
        qr = bp.aggregates(CntD(bp.b.RID).alias('num_biosamples_with_files')).fetch()
        res['biosample_with_file_count'] = qr[0]['num_biosamples_with_files']
        
    # file count
    if (counts is None) or ('file' in counts):
        fp = get_file_path()
        qr = fp.aggregates(CntD(fp.f.RID).alias('num_files')).fetch()
        res['file_count'] = qr[0]['num_files']

    # files describing subjects
    if (counts is None) or ('file_with_subject' in counts):
        fs = helper.builder.CFDE.file_describes_subject
        sp = get_file_path().link(fs)
        qr = sp.aggregates(CntD(sp.f.RID).alias('num_files_with_subjects')).fetch()
        res['file_with_subject_count'] = qr[0]['num_files_with_subjects']
    
    # files describing biosamples
    if (counts is None) or ('file_with_biosample' in counts):
        fb = helper.builder.CFDE.file_describes_biosample
        sp = get_file_path().link(fb)
        qr = sp.aggregates(CntD(sp.f.RID).alias('num_files_with_biosamples')).fetch()
        res['file_with_biosample_count'] = qr[0]['num_files_with_biosamples']

    return res

# /dcc/{dccName}/linkcount
# Returns the number of linked entities for various combinations.
@app.route('/dcc/<string:dcc_name>/linkcount', methods=['GET'])
def dcc_linkscount(dcc_name):
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    dcc = _abbreviation_to_dcc(helper, dcc_name)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_name)

    # DCC found
    res = _get_dcc_entity_counts(helper, dcc_name, None)
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
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

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

    dcc = _abbreviation_to_dcc(helper, dcc_name)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_name)

    vm = VARIABLE_MAP[variable]
    gm = GROUPING_MAP[grouping]
    counts = list(StatsQuery(helper).entity(vm['entity']).dimension(gm['dimension'],show_nulls=show_nulls).dimension('project_root',show_nulls=show_nulls).fetch())   
    res = {}
    
    for ct in counts:
        if ct['project_RID'] == dcc['RID']:
            nc = _get_stats_name_and_count(ct, gm['att'], vm['att'])
            res[nc['name']] = nc['count']

    # return type is DCCGrouping
    return json.dumps(res)

def _grouped_stats_aux(helper,variable,grouping1,max_groups1,grouping2,max_groups2):

    # need to map project_RID to project_abbreviation if grouping=dcc
    rid_to_abbrev = {}
    if (grouping1 == "dcc") or (grouping2 == "dcc"):
        dccs = list(helper.list_projects(use_root_projects=True))
        for dcc in dccs:
            rid_to_abbrev[dcc['RID']] = dcc['abbreviation']
    
    vm = VARIABLE_MAP[variable]
    gm1 = GROUPING_MAP[grouping1]
    gm2 = GROUPING_MAP[grouping2]
    
    counts = list(StatsQuery(helper).entity(vm['entity']).dimension(gm1['dimension'],show_nulls=show_nulls).dimension(gm2['dimension'],show_nulls=show_nulls).fetch())   

    dim1_counts = {}
    res = []

    # StatsQuery output looks like this:
    # {'anatomy_id': 'UBERON:0002387', 'species_id': 'NCBI:txid9606', 'num_subjects': 1, 'anatomy_name': 'pes', 'species_name': 'Homo sapiens'}
    # The following code aggregates/rewrites to this format (expected by dashboard):
    # {"anatomy": "pes", "Homo sapiens": 1}
    for ct in counts:
        dim1 = ct[gm1['att']]
        dim2 = ct[gm2['att']]

        if dim1 is None:
            dim1 = 'unknown'
        if dim2 is None:
            dim2 = 'unknown'
        
        # map RID to abbreviation if needed
        if (grouping1 == "dcc"):
            dim1 = rid_to_abbrev[dim1]
        if (grouping2 == "dcc"):
            dim2 = rid_to_abbrev[dim2]
        
        if not (dim1 in dim1_counts):
            dim1_counts[dim1] = { grouping1 : dim1 }
            res.append(dim1_counts[dim1])

        # replace None with 0
        if ct[vm['att']] is None:
            dim1_counts[dim1][dim2] = 0
        else:
            dim1_counts[dim1][dim2] = ct[vm['att']]

    return res
        
# TODO - factor out parameter error-checking code

# /dcc/stats/{variable}/{grouping}
# Returns statistics for the requested variable grouped by the specified aggregation.
@app.route('/stats/<string:variable>/<string:grouping1>/<string:grouping2>', methods=['GET'])
def grouped_stats(variable,grouping1,grouping2):
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

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

    res = _grouped_stats_aux(helper, variable, grouping1, None, grouping2, None)

    # return type is DCCGroupedStatistics, which is a list of DCCGrouping
    return json.dumps(res)

# merge attributes within groups using a global limit on the number of attributes
def _merge_within_groups_global(groups, max_atts, grouping1):
    # add up group2 counts across all DCCGroupings
    gcounts = {}

    for group in groups:
        for k in group:
            if k != grouping1:
                if k not in gcounts:
                    gcounts[k] = 0
                gcounts[k] += group[k]

    # sort groups and determine which to merge
    gsorted = sorted(list(gcounts.keys()), key=lambda x: int(gcounts[x]), reverse=True)

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

# merge attributes within groups using a local limit on the number of attributes
# (i.e., the total number of distinct attributes may exceed max_atts, theoretically by a lot)
def _merge_within_groups_local(groups, max_atts, grouping1):
    # apply mapping to groups
    new_groups = []

    for group in groups:
        new_group = {}
        atts = []
        
        # sort attributes by count
        for k in group:
            if k == grouping1:
                new_group[k] = group[k]
            else:
                atts.append({ 'att': k, 'count': group[k] })

        sorted_atts = sorted(atts, key=lambda x: x['count'], reverse=True)
        i = 0
        for att in [x['att'] for x in sorted_atts]:
            new_att = att
            if i >= max_atts:
                new_att = 'other'
            if new_att not in new_group:
                new_group[new_att] = 0
            new_group[new_att] += group[att]
            i += 1

        new_groups.append(new_group)
    
    return new_groups

# returns groups sorted by descending total count, even if len(groups) <= max_groups
def _merge_groups(groups, max_groups, grouping1):
    # sort groups by total count, retain the max_groups with the highest counts
    groups_w_count = []
    for group in groups:
        gwc = { 'group': group, 'total': 0}
        groups_w_count.append(gwc)
        for k in group:
            if k != grouping1:
                gwc['total'] += group[k]

    sorted_gwc = sorted(groups_w_count, key=lambda x: int(x['total']), reverse=True)
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
                if k == grouping1:
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
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

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
    res = _grouped_stats_aux(helper, variable, grouping1, maxgroups1, grouping2, maxgroups2)

    # merge groups2 (i.e., merge counts within each DCCGrouping)
    if maxgroups2 is not None:
        # enforce group2 maximum within each group independently
        res = _merge_within_groups_local(res, maxgroups2, grouping1)
        # enforce group2 maximum across groups
#        res = _merge_within_groups_global(res, maxgroups2, grouping1)
        
    # merge groups1 (i.e., merge GCCGroupings
    if maxgroups1 is not None:
        res = _merge_groups(res, maxgroups1, grouping1)
    
    # return type is DCCGroupedStatistics, which is a list of DCCGrouping
    return json.dumps(res)
