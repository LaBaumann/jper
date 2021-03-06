'''
This is the application scheduler. 
It defines scheduled tasks and runs them as per their defined schedule.

This scheduler is started and stopped when the app is started and stopped. 
Unless RUN_SCHEDULE is set to False in the config. In which case it must be started manually / managed by supervisor.
It is presumed to run on one machine at present.

If scaling later requires having multiple machines, then this scheduler should only run on the machine that has access to 
the relevant directories. There is a task that moves files from ftp user jail directories to tmp processing locations, and 
this is the limitation - creating sftp accounts has to happen on one machine or across machines, but that would increase 
attack surface for security vulnerability. So probably better to have only one machine open to sftp, and if necessary for 
later scale the script that is called to move data from the sftp jails to processing locations could do so by round-robin 
to multiple processing machines. The jper app config has settings for running this scheduler and what frequencies to run each 
process, so it is just a case of installing jper on each machine but only setting the frequencies for the processes desired to 
be scheduled on each given machine.

Or, if scheduled tasks themselves also need to be scaled up, the scheduler can continue to run on 
all machines but some synchronisation would have to be added to that tasks were not run on every machine. Also, each machine 
running the schedule would need access to any relevant directories.
'''

import schedule, time, os, shutil, requests, datetime, tarfile, zipfile, subprocess, getpass, uuid, json, csv
from threading import Thread
from octopus.core import app, initialise
from service import reports

import models, routing

# functions for the checkftp to unzip and move stuff up then zip again in incoming packages
def zip(src, dst):
    zf = zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED)
    abs_src = os.path.abspath(src)
    for dirname, subdirs, files in os.walk(src):
        for filename in files:
            absname = os.path.abspath(os.path.join(dirname, filename))
            arcname = absname[len(abs_src) + 1:]
            zf.write(absname, arcname)
    zf.close()

def extract(fl,path):
    app.logger.debug('Extracting ' + fl)
    try:
        # TODO the tar method has not yet been tested...
        tar = tarfile.open(fl)
        tar.extractall()
        tar.close()
        app.logger.debug('Extracted tar ' + fl)
        return True
    except:
        try:
            with zipfile.ZipFile(fl) as zf:
                for member in zf.infolist():
                    # Path traversal defense copied from
                    # http://hg.python.org/cpython/file/tip/Lib/http/server.py#l789
                    words = member.filename.split('/')
                    for word in words[:-1]:
                        drive, word = os.path.splitdrive(word)
                        head, word = os.path.split(word)
                        if word in (os.curdir, os.pardir, ''): continue
                        path = os.path.join(path, word)
                    zf.extract(member, path)
            app.logger.debug('Extracted zip ' + fl)
            return True
        except:
            app.logger.debug('Extraction could not be done for ' + fl)
            return False

def flatten(destination, depth=None):
    if depth is None:
        depth = destination
    app.logger.debug('Flatten depth set ' + destination + ' ' + depth)
    for fl in os.listdir(depth):
        app.logger.debug('Flatten at ' + fl)
        if '.zip' in fl: # or '.tar' in fl:
            app.logger.debug('Flatten ' + fl + ' is an archive')
            extracted = extract(depth + '/' + fl, depth)
            if extracted:
                app.logger.debug('Flatten ' + fl + ' is extracted')
                os.remove(depth + '/' + fl)
                flatten(destination,depth)
        elif os.path.isdir(depth + '/' + fl):
            app.logger.debug('Flatten ' + fl + ' is not a file, flattening')
            flatten(destination, depth + '/' + fl)
        else:
            try:
                shutil.move(depth + '/' + fl, destination)
            except:
                pass


def moveftp():
    try:
        # move any files in the jail of ftp users into the temp directory for later processing
        tmpdir = app.config.get('TMP_DIR','/tmp')
        userdir = app.config.get('USERDIR','/home/sftpusers')
        userdirs = os.listdir(userdir)
        app.logger.info("Scheduler - from FTP folders found " + str(len(userdirs)) + " user directories")
        for dir in userdirs:
            if len(os.listdir(userdir + '/' + dir + '/xfer')):
                for thisitem in os.listdir(userdir + '/' + dir + '/xfer'):
                    app.logger.info('Scheduler - moving file ' + thisitem + ' for Account:' + dir)
                    fl = os.path.dirname(os.path.abspath(__file__)) + '/models/moveFTPfiles.sh'
                    try:
                        newowner = getpass.getuser()
                    except:
                        newowner = 'mark'
                    uniqueid = uuid.uuid4().hex
                    uniquedir = tmpdir + '/' + dir + '/' + uniqueid
                    moveitem = userdir + '/' + dir + '/xfer/' + thisitem
                    subprocess.call( [ 'sudo', fl, dir, newowner, tmpdir, uniqueid, uniquedir, moveitem ] )
            else:
                app.logger.debug('Scheduler - found nothing to move for Account:' + dir)
    except:
        app.logger.error("Scheduler - move from FTP failed")
        
if app.config.get('MOVEFTP_SCHEDULE',10) != 0:
    schedule.every(app.config.get('MOVEFTP_SCHEDULE',10)).minutes.do(moveftp)

    
def processftp():
    try:
        # list all directories in the temp dir - one for each ftp user for whom files have been moved from their jail
        userdir = app.config.get('TMP_DIR','/tmp')
        userdirs = os.listdir(userdir)
        app.logger.debug("Scheduler - processing for FTP found " + str(len(userdirs)) + " temp user directories")
        for dir in userdirs:
            # configure for sending anything for the user of this dir
            apiurl = app.config['API_URL']
            acc = models.Account().pull(dir)
            apiurl += '?api_key=' + acc.data['api_key']
            # there is a uuid dir for each item moved in a given operation from the user jail
            for udir in os.listdir(userdir + '/' + dir):
                thisdir = userdir + '/' + dir + '/' + udir
                app.logger.debug('Scheduler - processing ' + thisdir + ' for Account:' + dir)
                for pub in os.listdir(thisdir):
                    # should be a dir per publication notification - that is what they are told to provide
                    # and at this point there should just be one pub in here, whether it be a file or directory or archive
                    # if just a file, even an archive, dump it into a directory so it can be zipped easily
                    if os.path.isfile(thisdir + '/' + pub):
                        nf = uuid.uuid4().hex
                        os.makedirs(thisdir + '/' + nf)
                        shutil.move(thisdir + '/' + pub, thisdir + '/' + nf + '/')
                        pub = nf
                    
                    # by now this should look like this:
                    # /Incoming/ftptmp/<useruuid>/<transactionuuid>/<uploadeddirORuuiddir>/<thingthatwasuploaded>

                    # they should provide a directory of files or a zip, but it could just be one file
                    # but we don't know the hierarchy of the content, so we have to unpack and flatten it all
                    # unzip and pull all docs to the top level then zip again. Should be jats file at top now
                    flatten(thisdir + '/' + pub)
                    pkg = thisdir + '/' + pub + '.zip'
                    zip(thisdir + '/' + pub, pkg)

                    # create a notification and send to the API to join the unroutednotification index
                    notification = {
                        "content": {"packaging_format": "https://pubrouter.jisc.ac.uk/FilesAndJATS"}
                    }
                    files = [
                        ("metadata", ("metadata.json", json.dumps(notification), "application/json")),
                        ("content", ("content.zip", open(pkg, "rb"), "application/zip"))
                    ]
                    app.logger.debug('Scheduler - processing POSTing ' + pkg + ' ' + json.dumps(notification))
                    resp = requests.post(apiurl, files=files, verify=False)
                    if str(resp.status_code).startswith('4') or str(resp.status_code).startswith('5'):
                        app.logger.error('Scheduler - processing completed with POST failure to ' + apiurl + ' - ' + str(resp.status_code) + ' - ' + resp.text)
                    else:
                        app.logger.info('Scheduler - processing completed with POST to ' + apiurl + ' - ' + str(resp.status_code))
                                            
                shutil.rmtree(userdir + '/' + dir + '/' + udir)
    except Exception as e:
        app.logger.error("Scheduler - failed scheduled process for FTP temp directories: '{x}'".format(x=e.message))

if app.config.get('PROCESSFTP_SCHEDULE',10) != 0:
    schedule.every(app.config.get('PROCESSFTP_SCHEDULE',10)).minutes.do(processftp)


def checkunrouted():
    urobjids = []
    robjids = []
    try:
        app.logger.debug("Scheduler - check for unrouted notifications")
        # query the service.models.unroutednotification index
        # returns a list of unrouted notification from the last three up to four months
        counter = 0
        for obj in models.UnroutedNotification.scroll():
            counter += 1
            res = routing.route(obj)
            if res:
                robjids.append(obj.id)
            else:
                urobjids.append(obj.id)
        app.logger.debug("Scheduler - routing sent " + str(counter) + " notifications for routing")
        if app.config.get("DELETE_ROUTED", False) and len(robjids) > 0:
            app.logger.debug("Scheduler - routing deleting " + str(len(robjids)) + " of " + str(counter) + " unrouted notifications that have been processed and routed")
            models.UnroutedNotification.bulk_delete(robjids)
        if app.config.get("DELETE_UNROUTED", False) and len(urobjids) > 0:
            app.logger.debug("Scheduler - routing deleting " + str(len(urobjids)) + " of " + str(counter) + " unrouted notifications that have been processed and were unrouted")
            models.UnroutedNotification.bulk_delete(urobjids)
    except Exception as e:
        app.logger.error("Scheduler - Failed scheduled check for unrouted notifications: '{x}'".format(x=e.message))

if app.config.get('CHECKUNROUTED_SCHEDULE',10) != 0:
    schedule.every(app.config.get('CHECKUNROUTED_SCHEDULE',10)).minutes.do(checkunrouted)



def monthly_reporting():
    # python schedule does not actually handle months, so this will run every day and check whether the current month has rolled over or not
    try:
        app.logger.debug('Scheduler - Running monthly reporting')
        
        # create / update a monthly deliveries by institution report
        # it should have the columns HEI, Jan, Feb...
        # and rows are HEI names then count for each month
        # finally ends with sum total (total of all numbers above) 
        # and unique total (total unique objects accessed - some unis may have accessed the same one)
        # query the retrieval index to see which institutions have retrieved content from the router in the last month
        
        month = datetime.datetime.now().strftime("%B")[0:3]
        year = str(datetime.datetime.now().year)
        app.logger.debug('Scheduler - checking monthly reporting for ' + month + ' ' + year)
        reportsdir = app.config.get('REPORTSDIR','/home/mark/jper_reports')
        if not os.path.exists(reportsdir): os.makedirs(reportsdir)
        monthtracker = reportsdir + '/monthtracker.cfg'
        try:
            lm = open(monthtracker,'r')
            lastmonth = lm.read().strip('\n')
            lm.close()
        except:
            lm = open(monthtracker,'w')
            lm.close()
            lastmonth = ''
            
        if lastmonth != month:
            app.logger.debug('Scheduler - updating monthly report of notifications delivered to institutions')
            lmm = open(monthtracker,'w')
            lmm.write(month)
            lmm.close()
        
            # get the month number that we are reporting on
            tmth = datetime.datetime.utcnow().month - 1

            # if the month is zero, it means the year just rolled over
            if tmth == 0:
                tmth = 12
                lastyear = int(year) - 1
                frm = str(lastyear) + "-" + str(tmth) + "-01T00:00:00Z"
                to_date = str(year) + "-01-01T00:00:00Z"
            else:
                mnthstr = str(tmth) if tmth > 9 else "0" + str(tmth)
                nexmnth = str(tmth + 1) if tmth + 1 > 9 else "0" + str(tmth + 1)
                frm = str(year) + "-" + mnthstr + "-01T00:00:00Z"
                if tmth == 12:
                    nextyear = int(year) + 1
                    to_date = str(nextyear) + "-01-01T00:00:00Z"
                else:
                    to_date = str(year) + "-" + nexmnth + "-01T00:00:00Z"

            # specify the file that we're going to output to
            reportfile = reportsdir + '/monthly_notifications_to_institutions_' + year + '.csv'

            # run the delivery report
            reports.delivery_report(frm, to_date, reportfile)

            # necessary tasks for other monthly reporting could be defined here
            # reporting that has to run more regularly could be defined as different reporting methods altogether
            # and controlled with different settings in the config
            
    except Exception as e:
        app.logger.error("Scheduler - Failed scheduled reporting job: '{x}'".format(x=e.message))
  
if app.config.get('SCHEDULE_MONTHLY_REPORTING',False):
    schedule.every().day.at("00:05").do(monthly_reporting)

    
def delete_old_routed():
    app.logger.info('Scheduler - checking for old routed indexes to delete')
    try:
        # each day send a delete to the index name that is beyond the range of those to keep
        # so only actually has an effect on the first day of each month - other days in the month it is sending a delete to an index that is already gone
        # index names look like routed201601
        # so read from config how many months to keep, and add 1 to it
        # so if in March, and keep is 3, then it becomes 4
        keep = app.config.get('SCHEDULE_KEEP_ROUTED_MONTHS',3) + 1
        year = datetime.datetime.utcnow().year
        # subtracting the keep gives us a month of -1 if now March
        month = datetime.datetime.utcnow().month - keep
        if month < 1:
            # so roll back the year, and set the month to 11 (if now March)
            year = year - 1
            month = 12 + month
        # so idx would look like routed201511 if now March - meaning we would keep Dec, Jan, and Feb (and Mar currently in use of course)
        idx = 'routed' + str(year) + str(month)
        addr = app.config['ELASTIC_SEARCH_HOST'] + '/' + app.config['ELASTIC_SEARCH_INDEX'] + '/' + idx
        app.logger.debug('Scheduler - sending delete to ' + addr)
        # send the delete - at the start of a month this would delete an index. Other days it will just fail
        requests.delete(addr)
    except Exception as e:
        app.logger.error("Scheduler - Failed monthly routed index deletion: '{x}'".format(x=e.message))

if app.config.get('SCHEDULE_DELETE_OLD_ROUTED',False):
    schedule.every().day.at("03:00").do(delete_old_routed)

    
def cheep():
    app.logger.debug("Scheduled cheep")
    print "Scheduled cheep"
#schedule.every(1).minutes.do(cheep)

def run():
    while True:
        schedule.run_pending()
        time.sleep(1)

def go():
    thread = Thread(target = run)
    thread.daemon = True
    thread.start()
    

if __name__ == "__main__":
    initialise()
    print "starting scheduler"
    app.logger.debug("Scheduler - starting up directly in own process.")
    run()
    
