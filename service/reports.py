"""
Functions which generate reports from the JPER system

"""

from service.models import RoutedNotification, Account
import os
from octopus.lib import clcsv
from copy import deepcopy
from datetime import datetime
from octopus.core import app

def delivery_report(from_date, to_date, reportfile):
    """
    Generate the monthly report from from_date to to_date.  It is assumed that from_date is
    the start of a month, and to_date is the end of a month.

    Dates must be strings of the form YYYY-MM-DDThh:mm:ssZ

    :param from_date:   start of month date from which to generate the report
    :param to_date: end of month date up to which to generate the report (if this is not specified, it will default to datetime.utcnow())
    :param reportfile:  file path for existing/new report to be output
    :return:
    """
    # work out the whole months that we're operating over
    frstamp = datetime.strptime(from_date, "%Y-%m-%dT%H:%M:%SZ")
    if to_date is None:
        tostamp = datetime.utcnow()
    else:
        tostamp = datetime.strptime(to_date, "%Y-%m-%dT%H:%M:%SZ")
    months = range(frstamp.month, tostamp.month + 1)

    # prep the data structures where we're going to record the results
    result = {}
    uniques = {}
    for m in months:
        uniques[m] = {"md" : 0, "content" : 0}
    heis = {}

    # go through each routed notification and count against the repository ids whether something is
    # a md-only or a with-content notification, and at the same time count the unique md-only vs with-content
    # notifications that were routed
    q = DeliveryReportQuery(from_date, to_date)
    for note in RoutedNotification.scroll(q.query(), page_size=100, keepalive="5m"):
        assert isinstance(note, RoutedNotification)
        nm = note.analysis_datestamp.month

        is_with_content = False
        if len(note.links) > 0:
            is_with_content = True
            uniques[nm]["content"] += 1
        else:
            uniques[nm]["md"] += 1

        for r in note.repositories:
            if r not in result:
                result[r] = {}
                for m in months:
                    result[r][m] = {"md" : 0, "content" : 0}

            if is_with_content:
                result[r][nm]["content"] += 1
            else:
                result[r][nm]["md"] += 1

    # now flesh out the report with account names and totals
    for k in result.keys():
        acc = Account.pull(k)
        if acc is None:
            heis[k] = k
        else:
            if acc.repository_name is not None:
                heis[k] = acc.repository_name
            else:
                heis[k] = k

        for mon in result[k].keys():
            result[k][mon]["total"] = result[k][mon]["md"] + result[k][mon]["content"]

    for mon in uniques.keys():
        uniques[mon]["total"] = uniques[mon]["md"] + uniques[mon]["content"]

    # some constant bits of information we're going to need to convert the results into a table
    # suitable for a CSV

    month_names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

    headers = ['HEI','ID',
               'Jan md-only', "Jan with-content", "Jan Total",
               'Feb md-only', "Feb with-content", "Feb Total",
               'Mar md-only', "Mar with-content", "Mar Total",
               'Apr md-only', "Apr with-content", "Apr Total",
               'May md-only', "May with-content", "May Total",
               'Jun md-only', "Jun with-content", "Jun Total",
               'Jul md-only', "Jul with-content", "Jul Total",
               'Aug md-only', "Aug with-content", "Aug Total",
               'Sep md-only', "Sep with-content", "Sep Total",
               'Oct md-only', "Oct with-content", "Oct Total",
               'Nov md-only', "Nov with-content", "Nov Total",
               'Dec md-only', "Dec with-content", "Dec Total"]

    template = {}
    for k in headers:
        template[k] = 0

    # an interim data-structure that we'll use to store the objects to be written, which we
    # can then order by the key (which will be the HEI name)
    data = {}

    # read any existing data in from the current spreadsheet
    if os.path.exists(reportfile):
        sofar = clcsv.ClCsv(file_path=reportfile)
        for obj in sofar.objects():
            # convert all the fields to integers as needed
            for k in obj.keys():
                if k not in ["HEI", "ID"]:
                    if obj[k] == "":
                        obj[k] = 0
                    else:
                        try:
                            obj[k] = int(obj[k])
                        except:
                            app.logger.warn(u"Unable to coerce existing report value '{x}' to an integer, so assuming it is 0".format(x=obj[k]))
                            obj[k] = 0

            data[obj.get("HEI")] = obj


    # now add any new data from the report
    for id, res in result.iteritems():
        hei = heis.get(id)
        if hei not in data:
            data[hei] = deepcopy(template)
        data[hei]["HEI"] = hei
        data[hei]["ID"] = id
        for mon, info in res.iteritems():
            mn = month_names[mon - 1]
            mdk = mn + " md-only"
            ctk = mn + " with-content"
            tk = mn + " Total"
            data[hei][mdk] = info.get("md")
            data[hei][ctk] = info.get("content")
            data[hei][tk] = info.get("total")

    # remove the "total" and "unique" entries, as we need to re-create them
    if "Total" in data:
        del data["Total"]
    existing_unique = deepcopy(template)
    existing_unique["HEI"] = "Unique"
    existing_unique["ID"] = ""
    if "Unique" in data:
        existing_unique = data["Unique"]
        del data["Unique"]

    # calculate the totals for all columns
    totals = {}
    for k in headers:
        totals[k] = 0

    totals["HEI"] = "Total"
    totals["ID"] = ""

    for hei, obj in data.iteritems():
        for k, v in obj.iteritems():
            if k in ["HEI", "ID"]:
                continue
            if isinstance(v, int):
                totals[k] += v

    data["Total"] = totals

    # add the uniques
    data["Unique"] = existing_unique
    data["Unique"]["HEI"] = "Unique"

    for mon, info in uniques.iteritems():
        mn = month_names[mon - 1]
        mdk = mn + " md-only"
        ctk = mn + " with-content"
        tk = mn + " Total"
        data["Unique"][mdk] = info.get("md")
        data["Unique"][ctk] = info.get("content")
        data["Unique"][tk] = info.get("total")

    orderedkeys = data.keys()
    orderedkeys.remove('Unique')
    orderedkeys.remove('Total')
    orderedkeys.sort()
    orderedkeys.append('Total')
    orderedkeys.append('Unique')

    # remove the old report file, so we can start with a fresh new one
    try:
        os.remove(reportfile)
    except:
        pass

    out = clcsv.ClCsv(file_path=reportfile)
    out.set_headers(headers)

    for hk in orderedkeys:
        hei = data[hk]
        out.add_object(hei)

    out.save()

class DeliveryReportQuery(object):
    def __init__(self, from_date, to_date):
        self.from_date = from_date
        self.to_date = to_date

    def query(self):
        return {
            "query" : {
                "bool" : {
                    "must" : [
                        {
                            "range" : {
                                "analysis_date" : {
                                    "gte" : self.from_date,
                                    "lt" : self.to_date
                                }
                            }
                        }
                    ]
                }
            },
            "sort" : [
                {"analysis_date" : {"order" :  "asc"}}
            ]
        }