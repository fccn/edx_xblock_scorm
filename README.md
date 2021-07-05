edx_xblock_scorm
=========================

Block displays SCORM which saved as `File -> Export -> Web Site -> Zip File`

Block displays SCORM which saved as `File -> Export -> SCORM 1.2`

Development
-----------

Clone this repository to the ~/src folder within your devstack.

Install this edx xblock scorm on both studio and lms devstack containers.

Studio:

    make studio-shell
    cd /edx/src/edx_xblock_scorm
    pip install -e .

LMS:

    make lms-shell
    cd /edx/src/edx_xblock_scorm
    pip install -e .


Installation
------------

Install package

    pip install -e git+https://github.com/raccoongang/edx_xblock_scorm.git#egg=edx_xblock_scorm

Add `scormxblock` to the list of advanced modules in the advanced settings of a course.
