import json
import os
import re
import atexit
import datetime
import urllib.parse
import requests
from requests.exceptions import HTTPError
from flask import Flask, request, make_response, wrappers
from cfde_deriva.dashboard_queries import StatsQuery2, DashboardQueryHelper
from deriva.core import DEFAULT_HEADERS, DEFAULT_SESSION_CONFIG, ErmrestCatalog
from deriva.core.utils import core_utils
from deriva.core.datapath import Min, Max, Cnt, CntD, Avg, Sum, Bin, DataPathException
from cfde_deriva.metrics import get_datapackage_measurements

app = Flask(__name__)
app.config.from_object('dashboard.dashboard_config')
CONFIG_FILE = os.path.join(os.path.expanduser('~'), 'dashboard.conf')
if os.path.isfile(CONFIG_FILE):
    app.config.from_pyfile(CONFIG_FILE)
SHOW_NULLS = app.config["SHOW_NULLS"]
PASS_HEADERS = app.config["PASS_HEADERS"]
HOSTNAME = app.config["DERIVA_SERVERNAME"]
DEFAULT_CATALOG_ID = app.config["DERIVA_DEFAULT_CATALOGID"]
webauthn_token = None if "DEV_TOKEN" not in app.config else app.config["DEV_TOKEN"]
helpers = {}

@atexit.register
def cleanup_helpers():
    for helper in helpers.values():
        helper.catalog._close_session()

def _error_response(err, code):
    res = make_response(err, code)
    res.headers['X-CFDE-Error'] = err.replace("\n", " ")
    return res

def _catalog_not_found_response(catalog_id):
    return _error_response("DERIVA catalogId " + str(catalog_id) + " not found", 404)

def _dcc_not_found_response(dcc_id):
    return _error_response("DCC '" + dcc_id + "' not found", 404)

@app.errorhandler(DataPathException)
def handle_datapath_exception(error):
    return _error_response(str(error),
                           error.reason.response.status_code if isinstance(error.reason, HTTPError) else 500)

def _get_scheme():
    return "http" if HOSTNAME == "localhost" else "https"

# Retrieve DashboardQueryHelper for the specified catalogid, using default catalogid if None.
#
def _get_helper(catalog_id):
    if catalog_id is None:
        catalog_id = DEFAULT_CATALOG_ID

    if isinstance(catalog_id, int):
        catalog_id = str(catalog_id)

    if catalog_id not in helpers:
        err = None
        try:
            helpers[catalog_id] = DashboardQueryHelper(HOSTNAME,
                                                       catalog_id,
                                                       scheme=_get_scheme(),
                                                       caching=False)
        # invalid catalog id
        except HTTPError as e:
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

def pass_headers():
    return dict(request.headers) if PASS_HEADERS else DEFAULT_HEADERS

# NOTE: there is no "RMT" system timestamp anymore for portal content records...
# options:
#  1. drop concept since it didn't really mean something for C2M2/DCCs
#  2. expose C2M2 creation_time which is nullable but could represent an event time in community
#  3. find some catalog-wide timestamp representing content load time?

def _id_to_dcc(helper, dcc_id):
    dcc = helper.builder.CFDE.dcc
    path = dcc.filter(dcc.id == dcc_id)

    res = path.attributes(
        path.dcc.id,
        path.dcc.nid,
        # RMT no longer exists
        path.dcc.dcc_name,
        path.dcc.dcc_description,
        path.dcc.dcc_url,
        path.dcc.contact_name,
        path.dcc.contact_email,
        path.dcc.dcc_abbreviation,
        path.dcc.project.alias('project_nid'),
    ).fetch(headers=pass_headers())

    if len(res) == 1:
        return res[0]

    return None

def _id_to_dcc_project_nid(helper, dcc_id):
    res = _id_to_dcc(helper, dcc_id)

    if res is not None:
        return res['project_nid']

    return None

def _id_to_dcc_nid(helper, dcc_id):
    res = _id_to_dcc(helper, dcc_id)

    if res is not None:
        return res['nid']

    return None

def _all_dccs(helper):
    path = helper.builder.CFDE.dcc.path

    res = path.attributes(
        path.dcc.id,
        path.dcc.nid,
        # RMT no longer exists
        path.dcc.dcc_name,
        path.dcc.dcc_description,
        path.dcc.dcc_url,
        path.dcc.contact_name,
        path.dcc.contact_email,
        path.dcc.dcc_abbreviation,
        path.dcc.project.alias("project_nid")
    ).fetch(headers=pass_headers())
    return res

def _decode_ermrest_snaptime(s):
    """Decode ERMrest's native snaptime to a timestamp

    A snaptime like "2WG-7RJ8-8F8P" is a custom base32 encoding of a
    64-bit timestamp. 13 symbols represent a 65 bit vector, and
    leading zero fields can be omitted.

    bits 1..64: integer most significant bit first
    bit 65: discarded pad bit

    the integer is the number of microseconds since 1970-1-1 epoch
    time.

    """
    epoch = datetime.datetime.fromisoformat('1970-01-01 00:00:00')
    one_second = datetime.timedelta(seconds=1)

    symbol_to_val = {
        '0123456789ABCDEFGHJKMNPQRSTVWXYZ'[i]: i
        for i in range(32)
    }
    # these are considered equivalent for transcription
    symbol_to_val.update({
        'O': 0,
        'I': 1,
        'L': 1,
    })

    s = s.replace('-', '')    # strip zero-info hyphens

    # decode 5 bits per symbol
    accum = 0
    for x in s:
        accum = (accum << 5) + symbol_to_val[x]

    # drop final pad bit
    accum = accum >> 1

    # interpret as microseconds since epoch
    ts = epoch + one_second * (accum / 1000000)
    return ts

def _ermrest_catalog_snaptime(helper):
    """Get and decode catalog snaptime

    GET /ermrest/catalog/{id} produces JSON like:

    { ..."snaptime": "2WF-XTV5-J226", ... }

    where the value can be decoded to a timestamp for the catalog's
    content as a whole

    """
    resp = helper.catalog.get('/').json()
    snaptime = resp['snaptime']
    return _decode_ermrest_snaptime(snaptime)

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

    dccs = _all_dccs(helper)
    dcc_list = [{
        'id': dcc['id'],
        'abbreviation': dcc['dcc_abbreviation'],
        'complete_name': dcc['dcc_name'],
        'description': dcc['dcc_description'],
        'url': dcc['dcc_url'],
        # 'last_updated': is not a C2M2 concept for DCCs!
        'nid': dcc['nid'],
    } for dcc in dccs]

    return json.dumps(dcc_list)

# /dcc_info
# Returns summary info for all DCCs in the archive, similar to /dcc/{dccId} for
# a single DCC.
@app.route('/dcc_info', methods=['GET'])
def all_dcc_info():
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    if catalog_id is None:
        catalog_id = DEFAULT_CATALOG_ID

    num_subjects = 0
    num_biosamples = 0
    num_files = 0
    num_projects = 0

    # subject, file, biosample, and project counts
    counts = _get_dcc_entity_counts(helper, None, { 'subject': True, 'file': True, 'biosample': True, 'project': True, 'anatomies': True, 'assay_types': True, 'disease' : True, 'gene' : True, 'compound' : True })
    # all DCCs
    dccs = _all_dccs(helper)

    # last_updated = max([ dcc.['last_updated'] for dcc in dccs ])

    return json.dumps({
        'catalog_id': catalog_id,
        'subject_count': counts['subject_count'],
        'biosample_count': counts['biosample_count'],
        'file_count': counts['file_count'],
        'project_count': counts['project_count'],
        'anatomy_count': counts['anatomy_count'],
        'assay_count': counts['assay_count'],
        'disease_count': counts['disease_count'],
        'gene_count': counts['gene_count'],
        'compound_count': counts['compound_count']
        #'last_updated': last_updated,
    })

# /dcc/{dccId}
# Returns general information about the specified DCC, such as the Principal Investigator(s) and description.
#
#      - id
#      - abbreviation
#      - complete_name
#      - description
#      - principal_investigators
#      - url
#      - project_count
#      - toplevel_project_count
#      - subject_count
#      - biosample_count
#      - file_count
#      - last_updated
#      - nid
#      - datapackage_RID
#
@app.route('/dcc/<string:dcc_id>', methods=['GET'])
def dcc_info(dcc_id):
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    if catalog_id is None:
        catalog_id = DEFAULT_CATALOG_ID

    dcc = _id_to_dcc(helper, dcc_id)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_id)

    # DCC found

    # subject, file, and biosample counts
    counts = _get_dcc_entity_counts(helper, dcc['nid'], { 'subject': True, 'file': True, 'biosample': True, 'project': True })

    # interrogate registry for datapackage RID
    session_config = DEFAULT_SESSION_CONFIG.copy()
    session_config["allow_retry_on_all_methods"] = True
    r_catalog = ErmrestCatalog(_get_scheme(), HOSTNAME, 'registry', caching=False, session_config=session_config)
    r_builder = r_catalog.getPathBuilder()
    r_schema = r_catalog.getCatalogModel().schemas['CFDE']

    # TODO - use a more direct approach, if possible:
    dp_path = r_builder.CFDE.datapackage
    dp_path = dp_path.filter(dp_path.review_summary_url.regexp('catalogId=%s$' % (catalog_id,)))
    res = dp_path.entities().fetch(headers=pass_headers())

    dp_rid = None
    last_updated = None    
        
    if res:
        dp_rid = res[0]['RID']
        last_updated = res[0]['submission_time']

    return json.dumps({
        'id': dcc['id'],
        'abbreviation': dcc['dcc_abbreviation'],
        'complete_name': dcc['dcc_name'],
        'description': dcc['dcc_description'],
        # TODO - current schema has primary contact, but no explicit info on PIs
        'principal_investigators': [],
        'url': dcc['dcc_url'],
        'project_count': counts['project_count'],
        'toplevel_project_count': counts['toplevel_project_count'],
        'subject_count': counts['subject_count'],
        'biosample_count': counts['biosample_count'],
        'file_count': counts['file_count'],
        'last_updated': last_updated,
        'nid': dcc['nid'],
        'datapackage_RID': dp_rid,
    })

# /dcc/{dccId}/projects
# Returns a listing of (top-level) projects associated with the specified DCC.
@app.route('/dcc/<string:dcc_id>/projects', methods=['GET'])
def dcc_projects(dcc_id):
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    dcc_proj_nid = _id_to_dcc_project_nid(helper, dcc_id)

    # DCC not found
    if dcc_proj_nid is None:
        return _dcc_not_found_response(dcc_id)

    # DCC found
    projects = helper.list_projects(parent_project_nid=dcc_proj_nid)
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

# /dcc/{dccId}/filecount
# Returns the number of files associated with a particular DCC broken down by data type.
@app.route('/dcc/<string:dcc_id>/filecount', methods=['GET'])
def dcc_filecount(dcc_id):
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    dcc_nid = _id_to_dcc_nid(helper, dcc_id)
    
    # DCC not found
    if dcc_nid is None:
        return _dcc_not_found_response(dcc_id)

    # DCC found
    fcounts = list(StatsQuery2(helper).entity('file').dimension('data_type').dimension('dcc').fetch_flattened(headers=pass_headers()))
    res = {}

    for fc in fcounts:
        if fc['dcc'] is None:
            pass
        elif fc['dcc']['nid'] == dcc_nid:
            key = None
            if fc['data_type'] is None:
                key = 'Not Specified'
            else:
                key = fc['data_type']['name']

            if key in res:
                res[key] += fc['num_files']
            else:
                res[key] = fc['num_files']

    return json.dumps(res)

# don't filter by DCC if dcc_nid is None
def _get_dcc_entity_counts(helper, dcc_nid, counts):
    res = {}

    # get path to all subprojects of DCC
    def get_proj_path():
        dcc = helper.builder.CFDE.dcc.alias('dcc')
        p_root = helper.builder.CFDE.project_root.alias('p_root')
        p = helper.builder.CFDE.project.alias('p')
        pipt = helper.builder.CFDE.project_in_project_transitive.alias('pipt')
        if dcc_nid is None:
            path = p_root.link(p)
        else:
            path = dcc.filter(dcc.nid == dcc_nid).link(p)
        proj_path = path.link(pipt, on=(p.nid == pipt.leader_project))
        return proj_path

    # get path to only top-level subprojects of a root project (i.e., DCC)
    def get_subproj_path():
        dcc = helper.builder.CFDE.dcc.alias('dcc')
        p_root = helper.builder.CFDE.project_root.alias('p_root')
        p = helper.builder.CFDE.project.alias('p')
        pip = helper.builder.CFDE.project_in_project.alias('pip')
        if dcc_nid is None:
            path = p_root.link(p)
        else:
            path = dcc.filter(dcc.nid == dcc_nid).link(p)
        proj_path = path.link(pip, on=(p.nid == pip.parent_project))
        return proj_path

    # path to DCCs' subjects
    def get_subj_path():
        proj_path = get_proj_path()
        s = helper.builder.CFDE.subject.alias('s')
        cf = helper.builder.CFDE.core_fact.alias('cf')
        subj_path = proj_path.link(cf, on=(proj_path.pipt.member_project == cf.project)).link(s)
        return subj_path

    # path to DCCs' biosamples
    def get_biosample_path():
        proj_path = get_proj_path()
        b = helper.builder.CFDE.biosample.alias('b')
        cf = helper.builder.CFDE.core_fact.alias('cf')
        biosample_path = proj_path.link(cf, on=(proj_path.pipt.member_project == cf.project)).link(b)
        return biosample_path

    # path to DCCs' files
    def get_file_path():
        proj_path = get_proj_path()
        f = helper.builder.CFDE.file.alias('f')
        cf = helper.builder.CFDE.core_fact.alias('cf')
        file_path = proj_path.link(cf, on=(proj_path.pipt.member_project == cf.project)).link(f)
        return file_path

    # project counts - all and only children of top-level DCC project node
    if (counts is None) or ('project' in counts):
        pp = get_proj_path()
        qr = pp.aggregates(CntD(pp.pipt.member_project).alias('num_projects')).fetch(headers=pass_headers())
        res['project_count'] = qr[0]['num_projects'] - 1

        sp = get_subproj_path()
        qr = sp.aggregates(CntD(sp.pip.child_project).alias('num_projects')).fetch(headers=pass_headers())
        res['toplevel_project_count'] = qr[0]['num_projects']

    # subject count
    if (counts is None) or ('subject' in counts):
        sp = get_subj_path()
        qr = sp.aggregates(CntD(sp.s.nid).alias('num_subjects')).fetch(headers=pass_headers())
        res['subject_count'] = qr[0]['num_subjects']

    # subjects linked to biosamples
    if (counts is None) or ('subject_with_biosample' in counts):
        bs = helper.builder.CFDE.biosample_from_subject
        sp = get_subj_path().link(bs)
        qr = sp.aggregates(CntD(sp.s.nid).alias('num_subjects_with_biosamples')).fetch(headers=pass_headers())
        res['subject_with_biosample_count'] = qr[0]['num_subjects_with_biosamples']

    # subjects linked to files
    if (counts is None) or ('subject_with_file' in counts):
        fs = helper.builder.CFDE.file_describes_subject
        sp = get_subj_path().link(fs)
        qr = sp.aggregates(CntD(sp.s.nid).alias('num_subjects_with_files')).fetch(headers=pass_headers())
        res['subject_with_file_count'] = qr[0]['num_subjects_with_files']

    # biosample count
    if (counts is None) or ('biosample' in counts):
        bp = get_biosample_path()
        qr = bp.aggregates(CntD(bp.b.nid).alias('num_biosamples')).fetch(headers=pass_headers())
        res['biosample_count'] = qr[0]['num_biosamples']

    # biosamples linked to subjects
    if (counts is None) or ('biosample_with_subject' in counts):
        bp = get_biosample_path().link(bs)
        qr = bp.aggregates(CntD(bp.b.nid).alias('num_biosamples_with_subjects')).fetch(headers=pass_headers())
        res['biosample_with_subject_count'] = qr[0]['num_biosamples_with_subjects']

    # biosamples about which files were produced
    if (counts is None) or ('biosample_with_file' in counts):
        fb = helper.builder.CFDE.file_describes_biosample
        bp = get_biosample_path().link(fb)
        qr = bp.aggregates(CntD(bp.b.nid).alias('num_biosamples_with_files')).fetch(headers=pass_headers())
        res['biosample_with_file_count'] = qr[0]['num_biosamples_with_files']

    # file count
    if (counts is None) or ('file' in counts):
        fp = get_file_path()
        qr = fp.aggregates(CntD(fp.f.nid).alias('num_files')).fetch(headers=pass_headers())
        res['file_count'] = qr[0]['num_files']

    # files describing subjects
    if (counts is None) or ('file_with_subject' in counts):
        fs = helper.builder.CFDE.file_describes_subject
        sp = get_file_path().link(fs)
        qr = sp.aggregates(CntD(sp.f.nid).alias('num_files_with_subjects')).fetch(headers=pass_headers())
        res['file_with_subject_count'] = qr[0]['num_files_with_subjects']

    # files describing biosamples
    if (counts is None) or ('file_with_biosample' in counts):
        fb = helper.builder.CFDE.file_describes_biosample
        sp = get_file_path().link(fb)
        qr = sp.aggregates(CntD(sp.f.nid).alias('num_files_with_biosamples')).fetch(headers=pass_headers())
        res['file_with_biosample_count'] = qr[0]['num_files_with_biosamples']

    if (counts is None) or ('anatomies' in counts):
        an = helper.builder.CFDE.anatomy.alias("anatomy_alias")
        qr = an.entities().fetch(headers=pass_headers())
        res['anatomy_count'] = len(qr)

    if (counts is None) or ('assay_types' in counts):
        at = helper.builder.CFDE.assay_type.alias("assay_types_alias")
        qr = at.entities().fetch(headers=pass_headers())
        res['assay_count'] = len(qr)

    if (counts is None) or ('disease' in counts):
        di = helper.builder.CFDE.disease.alias("disease_alias")
        qr = di.entities().fetch(headers=pass_headers())
        res['disease_count'] = len(qr)

    if (counts is None) or ('gene' in counts):
        ge = helper.builder.CFDE.gene.alias("gene_alias")
        qr = ge.entities().fetch(headers=pass_headers())
        res['gene_count'] = len(qr)

    if (counts is None) or ('compound' in counts):
        co = helper.builder.CFDE.compound.alias("compound_alias")
        qr = co.entities().fetch(headers=pass_headers())
        res['compound_count'] = len(qr)
    return res

# /dcc/{dccId}/linkcount
# Returns the number of linked entities for various combinations.
@app.route('/dcc/<string:dcc_id>/linkcount', methods=['GET'])
def dcc_linkscount(dcc_id):
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    dcc = _id_to_dcc(helper, dcc_id)

    # DCC not found
    if dcc is None:
        return _dcc_not_found_response(dcc_id)

    # DCC found
    res = _get_dcc_entity_counts(helper, dcc['nid'], None)
    res['nid'] = dcc['nid']
    return json.dumps(res)

# StatsQuery2 parameterization for dcc_grouped_stats
SQ2_ENTITY_MAP = {
    'file': { 'att': 'num_files' },
    'collection': { 'att': 'num_collections' },
    'biosample': { 'att': 'num_biosamples' },
    'subject': { 'att': 'num_subjects' },
}
SQ2_DIMENSION_MAP = {
    'dcc': { 'att': 'dcc_abbreviation' },
    'analysis_type': { 'att': 'name' },
    'anatomy':  { 'att': 'name' },
    'assay_type':  { 'att': 'name' },
    'compression_format':  { 'att': 'name' },
    'data_type':  { 'att': 'name' },
    'disease':  { 'att': 'name' },
    'ethnicity':  { 'att': 'name' },
    'file_format':  { 'att': 'name' },
    'gene':  { 'att': 'id' },
    'mime_type': { 'att': 'id' },
    'ncbi_taxonomy':  { 'att': 'name' },
    'phenotype':  { 'att': 'name' },
    'protein':  { 'att': 'name' },
    'race':  { 'att': 'name' },
    'sample_prep_method':  { 'att': 'name' },
    'sex':  { 'att': 'name' },
    'species':  { 'att': 'name' },
    'substance':  { 'att': 'name' },
    'subject_granularity':  { 'att': 'name' },
    'subject_role':  { 'att': 'name' },
}

# /dcc/{dccId}/stats/{variable}/{grouping}
# Returns statistics for the requested variable grouped by the specified aggregation.
@app.route('/dcc/<string:dcc_id>/stats/<string:variable>/<string:grouping>', methods=['GET'])
def dcc_grouped_stats(dcc_id,variable,grouping):
    catalog_id = request.args.get("catalogId", type=int)
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    err = None

    if variable not in SQ2_ENTITY_MAP:
        err = "Illegal variable/entity requested - must be one of " + ",".join(SQ2_ENTITY_MAP.keys())

    if grouping not in SQ2_DIMENSION_MAP:
        err = "Illegal grouping requested - must be one of " + ",".join(SQ2_DIMENSION_MAP.keys())

    # input error
    if err is not None:
        return _error_response(err, 404)

    dcc_nid = _id_to_dcc_nid(helper, dcc_id)

    # DCC not found
    if dcc_nid is None:
        return _dcc_not_found_response(dcc_id)

    em = SQ2_ENTITY_MAP[variable]
    dm = SQ2_DIMENSION_MAP[grouping]

    counts = list(StatsQuery2(helper).entity(variable).dimension(grouping).dimension('dcc').fetch_flattened(headers=pass_headers()))
    res = {}

    for ct in counts:
        if ct['dcc'] is None:
            pass
        elif ct['dcc']['nid'] == dcc_nid:
            key = None
            if ct[grouping] is None:
                key = 'Not Specified'
            else:
                key = ct[grouping][dm['att']]

            if key in res:
                res[key] += ct[em['att']]
            else:
                res[key] = ct[em['att']]

    # return type is DCCGrouping
    return json.dumps(res)

def _grouped_stats_aux(helper,variable,grouping1,grouping2,add_dcc):
    em = SQ2_ENTITY_MAP[variable]
    dm1 = SQ2_DIMENSION_MAP[grouping1]
    dm2 = SQ2_DIMENSION_MAP[grouping2]
    grouping3 = None
    dm3 = None

    if add_dcc and grouping1 != 'dcc':
        grouping3 = 'dcc'
        dm3 = SQ2_DIMENSION_MAP[grouping3]

    # need to map project_nid to project_abbreviation if grouping=dcc
    nid_to_abbrev = {}
    if (grouping1 == "dcc") or (grouping2 == "dcc") or (grouping3 == "dcc"):
        dccs = _all_dccs(helper)
        for dcc in dccs:
            nid_to_abbrev[dcc['project_nid']] = dcc['dcc_abbreviation']

    sh = StatsQuery2(helper).entity(variable).dimension(grouping1).dimension(grouping2)

    if dm3 is not None and grouping2 != 'dcc':
        sh = sh.dimension(grouping3)
    
    counts = list(sh.fetch_flattened(headers=pass_headers()))
    dim_counts = {}
    res = []

    # StatsQuery2 output looks like this:
    #
    # {'num_files': 789,
    #  'total_size_in_bytes': 970063224,
    #  'dcc': {'nid': 1,
    #          'id': 'cfde_registry_dcc:sparc',
    #          'dcc_name': 'Stimulating Peripheral Activity to Relieve Conditions',
    #          'dcc_abbreviation': 'SPARC',
    #          'dcc_description': 'To transform our understanding of nerve-organ interactions
    #             by providing access to high-value datasets, maps, and computational studies
    #             with the intent of advancing bioelectronic medicine towards treatments that
    #             change lives. The SPARC program is supported by the NIH Common Fund to
    #             accelerate development of therapeutic devices and identification of neural
    #             targets for bioelectronic medicine—modulating electrical activity in nerves
    #             to help treat diseases and conditions, such as hypertension and gastrointestinal
    #             disorders, by precisely adjusting organ function.'},
    #  'assay_type': None}
    #
    for ct in counts:
        dim1 = ct[grouping1]
        if dim1 is not None:
            dim1 = dim1[dm1['att']]
        
        dim2 = ct[grouping2]
        if dim2 is not None:
            dim2 = dim2[dm2['att']]

        dim3 = None
        if dm3 is not None:
            if grouping2 == 'dcc':
                dim3 = dim2
            else:
                dim3 = ct[grouping3]

        if dim1 is None:
            dim1 = 'Not Specified'
        if dim2 is None:
            dim2 = 'Not Specified'

        key = dim1
        if dim3 is not None:
            key = dim1 + ":" + dim3

        if not (key in dim_counts):
            dim_counts[key] = { grouping1 : dim1 }
            if dim3 is not None:
                dim_counts[key][grouping3] = dim3
            res.append(dim_counts[key])

        # replace None with 0
        ctval = 0
        if ct[em['att']] is not None:
            ctval  = ct[em['att']]

        if dim2 in dim_counts[key]:
            dim_counts[key][dim2] += ctval
        else:
            dim_counts[key][dim2] = ctval

    return res
        
# TODO - factor out parameter error-checking code

# /stats/{variable}/{grouping1}/{grouping2}
# Returns statistics for the requested variable grouped by the specified aggregation.
@app.route('/stats/<string:variable>/<string:grouping1>/<string:grouping2>', methods=['GET'])
def grouped_stats_by_dcc(variable,grouping1,grouping2):
    catalog_id = request.args.get("catalogId", type=int)
    include_dcc = request.args.get("includeDCC") == "true"
    helper = _get_helper(catalog_id)
    if isinstance(helper, wrappers.Response):
        return helper

    err = None

    if variable not in SQ2_ENTITY_MAP:
        err = "Illegal variable/entity requested - must be one of " + ",".join(SQ2_ENTITY_MAP.keys())
    if grouping1 not in SQ2_DIMENSION_MAP:
        err = "Illegal grouping requested - must be one of " + ",".join(SQ2_DIMENSION_MAP.keys())
    if grouping2 not in SQ2_DIMENSION_MAP:
        err = "Illegal grouping requested - must be one of " + ",".join(SQ2_DIMENSION_MAP.keys())
    if grouping1 == grouping2:
        err = "grouping1 and grouping2 cannot be the same dimension."

    # input error
    if err is not None:
        return _error_response(err, 404)

    # return type is DCCGroupedStatistics, which is a list of DCCGrouping
    res = _grouped_stats_aux(helper, variable, grouping1, grouping2, include_dcc)
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

    if variable not in SQ2_ENTITY_MAP:
        err = "Illegal variable/entity requested - must be one of " + ",".join(SQ2_ENTITY_MAP.keys())
    if grouping1 not in SQ2_DIMENSION_MAP:
        err = "Illegal grouping requested - must be one of " + ",".join(SQ2_DIMENSION_MAP.keys())
    if grouping2 not in SQ2_DIMENSION_MAP:
        err = "Illegal grouping requested - must be one of " + ",".join(SQ2_DIMENSION_MAP.keys())
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
    res = _grouped_stats_aux(helper, variable, grouping1, grouping2, False)

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

def _get_user_id():
    url = _get_scheme() + "://" + HOSTNAME + "/authn/session"
    id = None
    r = requests.get(url=url, headers=pass_headers())
    if r:
        if r.status_code != 404 and r.json():
            data = r.json()
            try:
                id = data["client"]["id"]
            except KeyError:
                pass
    return id

# /user/saved_queries
# Returns a list of saved queries for the logged in user
# User auth maintained by headers being passed through. See pass_headers()
@app.route('/user/saved_queries', methods=['GET'])
def saved_queries():
    user_id = _get_user_id()
    scheme = "http" if HOSTNAME == "localhost" else "https"
    dev_mode = True if webauthn_token else False
    
    if dev_mode:
        registry_catalog = ErmrestCatalog(
            scheme,
            HOSTNAME,
            "registry",
            caching=False,
            credentials=core_utils.format_credential(token=webauthn_token)
        )
    else:
        registry_catalog = ErmrestCatalog(
            scheme,
            HOSTNAME,
            "registry",
            caching=False
        )

    registry_builder = registry_catalog.getPathBuilder()
    saved_query = registry_builder.CFDE.saved_query
    if not dev_mode:
        path = saved_query.filter(saved_query.user_id == user_id)
    else:
        path = saved_query

    # if running in a dev workspace (dev_mode == True), not using pass_headers call
    if dev_mode:
        rows = path.entities().fetch()
    else:
        # sending headers because we're not instantiating the ermrest catalog with a user credential
        rows = path.entities().fetch(headers=pass_headers())
   
    nonempty_query_url_string = "/chaise/recordset/#1/{}:{}/*::facets::{}?savedQueryRid={}"
    empty_query_url_string = "/chaise/recordset/#1/{}:{}?savedQueryRid={}"

    saved_queries = []

    for row in rows:
        if row['encoded_facets']: 
           query = nonempty_query_url_string.format(row["schema_name"], row["table_name"], row["encoded_facets"], row["RID"])
        else:
           query = empty_query_url_string.format(row["schema_name"], row["table_name"], row["RID"])

        data = { "name" : row["name"],
                 "table_name" : row["table_name"], 
                 "description" : row["description"], 
                 "query" : query,
                 "last_execution_ts" : row["last_execution_time"],
                 "creation_ts" : row["RCT"] 
        }
        saved_queries.append(data)

    return json.dumps(saved_queries)


def _fetch_favorite(path, url_string, dev_mode, include_abbreviation=False):

    # if running in a dev workspace (dev_mode == True), not using pass_headers call
    if dev_mode:
        rows = path.entities().fetch()
    else:
        # sending headers because we're not instantiating the ermrest catalog with a user credential
        rows = path.entities().fetch(headers=pass_headers())
    
    favorite_data = [ { "id" : row["id"],
                        "name" : row["name"] if "name" in row else row["dcc_name"],
                        "description" : row["description"],
                        "abbreviation" : row["dcc_abbreviation"] if include_abbreviation else None,
                        "url" : url_string.format(urllib.parse.quote(row["id"])) } for row in rows]
    return favorite_data

@app.route('/user/personal_collections', methods=['GET'])
def personal_collection():
    
    dev_mode = True if webauthn_token else False

    user_id = _get_user_id()
    scheme = "http" if HOSTNAME == "localhost" else "https"
    
    if dev_mode:
        catalog = ErmrestCatalog(
            scheme,
            HOSTNAME,
            DEFAULT_CATALOG_ID,
            caching=False,
            credentials=core_utils.format_credential(token=webauthn_token)
        )
    else:
        catalog = ErmrestCatalog(
            scheme,
            HOSTNAME,
            DEFAULT_CATALOG_ID,
            caching=False
        )

    builder = catalog.getPathBuilder()
    path = builder.CFDE.personal_collection

    if dev_mode:
        rows = path.entities().fetch()
    else:
        # sending headers because we're not instantiating the ermrest catalog with a user credential
        path = path.filter(path.RCB == user_id)
        rows = path.entities().fetch(headers=pass_headers())
    
    personal_collection_url = "/chaise/record/#1/CFDE:personal_collection/RID={}"

    personal_collections = []
    for row in rows:
        data = { "name" : row["name"],
                 "description" : row["description"], 
                 "creation_ts" : row["RCT"],
                 "query" : personal_collection_url.format(row["RID"])
        }
        personal_collections.append(data)

    return json.dumps(personal_collections)

# /user/favorites
# User auth maintained by headers being passed through. See pass_headers()
@app.route('/user/favorites', methods=['GET'])
def favorites():

    dev_mode = True if webauthn_token else False

    user_id = _get_user_id()
    scheme = "http" if HOSTNAME == "localhost" else "https"
    
    if dev_mode:
        registry_catalog = ErmrestCatalog(
            scheme,
            HOSTNAME,
            "registry",
            caching=False,
            credentials=core_utils.format_credential(token=webauthn_token)
        )
    else:
        registry_catalog = ErmrestCatalog(
            scheme,
            HOSTNAME,
            "registry",
            caching=False
        )

    # build path (query) for favorite anatomies
    registry_builder = registry_catalog.getPathBuilder()

    # This repetitive bit of code could be cleaned up
    url_string = "/chaise/record/#1/CFDE:anatomy/id={}"
    path = registry_builder.CFDE.favorite_anatomy
    path = path.link(registry_builder.CFDE.anatomy) # links in the anatomy record for each favorite including: id, name, description
    if not dev_mode:
        path = path.filter(path.favorite_anatomy.user_id == user_id)
    favorite_anatomies = _fetch_favorite(path, url_string, dev_mode)

    url_string = "/chaise/record/#1/CFDE:dcc/id={}"
    path = registry_builder.CFDE.favorite_dcc
    path = path.link(registry_builder.CFDE.dcc)
    if not dev_mode:
        path = path.filter(path.favorite_dcc.user_id == user_id)
    favorite_dccs = _fetch_favorite(path, url_string, dev_mode, include_abbreviation=True)

    url_string = "/chaise/record/#1/CFDE:assay_type/id={}"
    path = registry_builder.CFDE.favorite_assay_type
    path = path.link(registry_builder.CFDE.assay_type)
    if not dev_mode:
        path = path.filter(path.favorite_assay_type.user_id == user_id)
    favorite_assays = _fetch_favorite(path, url_string, dev_mode)

    url_string = "/chaise/record/#1/CFDE:disease/id={}"
    path = registry_builder.CFDE.favorite_disease
    path = path.link(registry_builder.CFDE.disease)
    if not dev_mode:
        path = path.filter(path.favorite_disease.user_id == user_id)
    favorite_diseases = _fetch_favorite(path, url_string, dev_mode)
    
    url_string = "/chaise/record/#1/CFDE:ncbi_taxonomy/id={}"
    path = registry_builder.CFDE.favorite_ncbi_taxonomy
    path = path.link(registry_builder.CFDE.ncbi_taxonomy)
    if not dev_mode:
        path = path.filter(path.favorite_ncbi_taxonomy.user_id == user_id)
    favorite_taxa = _fetch_favorite(path, url_string, dev_mode)
    
    url_string = "/chaise/record/#1/CFDE:data_type/id={}"
    path = registry_builder.CFDE.favorite_data_type
    path = path.link(registry_builder.CFDE.data_type)
    if not dev_mode:
        path = path.filter(path.favorite_data_type.user_id == user_id)
    favorite_data_types = _fetch_favorite(path, url_string, dev_mode)

    url_string = "/chaise/record/#1/CFDE:file_format/id={}"
    path = registry_builder.CFDE.favorite_file_format
    path = path.link(registry_builder.CFDE.file_format)
    if not dev_mode:
        path = path.filter(path.favorite_file_format.user_id == user_id)
    favorite_file_formats = _fetch_favorite(path, url_string, dev_mode)

    url_string = "/chaise/record/#1/CFDE:gene/id={}"
    path = registry_builder.CFDE.favorite_gene
    path = path.link(registry_builder.CFDE.gene)
    if not dev_mode:
        path = path.filter(path.favorite_gene.user_id == user_id)
    favorite_genes = _fetch_favorite(path, url_string, dev_mode)

    url_string = "/chaise/record/#1/CFDE:compound/id={}"
    path = registry_builder.CFDE.favorite_compound
    path = path.link(registry_builder.CFDE.compound)
    if not dev_mode:
        path = path.filter(path.favorite_compound.user_id == user_id)
    favorite_compounds = _fetch_favorite(path, url_string, dev_mode)

    url_string = "/chaise/record/#1/CFDE:analysis_type/id={}"
    path = registry_builder.CFDE.favorite_analysis_type
    path = path.link(registry_builder.CFDE.analysis_type)
    if not dev_mode:
        path = path.filter(path.favorite_analysis_type.user_id == user_id)
    favorite_analysis_types = _fetch_favorite(path, url_string, dev_mode)

    url_string = "/chaise/record/#1/CFDE:phenotype/id={}"
    path = registry_builder.CFDE.favorite_phenotype
    path = path.link(registry_builder.CFDE.phenotype)
    if not dev_mode:
        path = path.filter(path.favorite_phenotype.user_id == user_id)
    favorite_phenotypes = _fetch_favorite(path, url_string, dev_mode)

    url_string = "/chaise/record/#1/CFDE:protein/id={}"
    path = registry_builder.CFDE.favorite_protein
    path = path.link(registry_builder.CFDE.protein)
    if not dev_mode:
        path = path.filter(path.favorite_protein.user_id == user_id)
    favorite_proteins = _fetch_favorite(path, url_string, dev_mode)

    return_obj = { "anatomy" : favorite_anatomies,
                   "dcc" : favorite_dccs,
                   "assay" : favorite_assays,
                   "disease" : favorite_diseases,
                   "taxon" : favorite_taxa,
                   "data_type" : favorite_data_types,
                   "file_format" : favorite_file_formats,
                   "gene": favorite_genes,
                   "compound": favorite_compounds,
                   "analysis_type": favorite_analysis_types,
                   "phenotype": favorite_phenotypes,
                   "protein": favorite_proteins
    }

    return json.dumps(return_obj)

# Gets FAIRMetrics for specified catalog
# Each metric is a name, fair_count, total_count and comment
# The metric score is calculated as fair_count / total_count
@app.route('/fair/<int:catalog_id>', methods=['GET'])
def fair_metrics(catalog_id):
    
    session_config = DEFAULT_SESSION_CONFIG.copy()
    session_config["allow_retry_on_all_methods"] = True
    scheme = "http" if HOSTNAME == "localhost" else "https"
    
    dev_mode = True if webauthn_token else False

    if dev_mode:
        registry_catalog = ErmrestCatalog(
            scheme,
            HOSTNAME,
            "registry",
            caching=False,
            credentials=core_utils.format_credential(token=webauthn_token)
        )
    else:
        registry_catalog = ErmrestCatalog(
            scheme,
            HOSTNAME,
            "registry",
            caching=False
        )
    
    # Need to get the submission ID by using the catalog_id
    r_builder = registry_catalog.getPathBuilder()
    dp_path = r_builder.CFDE.datapackage
    dp_path = dp_path.filter(dp_path.review_summary_url.regexp('catalogId=%s$' % (catalog_id,)))
    submission_id = ''
    
    if dev_mode:
       res = dp_path.entities().fetch()
    else:
       res = dp_path.entities().fetch(headers=pass_headers())
    
    if res:
        submission_id = res[0]['id']
    
    if dev_mode:
        data = get_datapackage_measurements(registry_catalog, submission_id)
    else:
        # sending headers because we're not instantiating the ermrest catalog with a user credential
        data = get_datapackage_measurements(registry_catalog, submission_id, headers=pass_headers())

    fair = []
    for metric_dict in data:
        metric = {
            "id" : metric_dict["metric"],
            "name": metric_dict["name"],
            "fair_count": metric_dict["numerator"],
             "total_count": metric_dict["denominator"]
        }
        fair.append(metric)

    return json.dumps(fair)


if __name__ == '__main__':
    app.run(threaded=True)
